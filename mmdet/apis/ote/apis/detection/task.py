# Copyright (C) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.

import copy
import io
import os
import shutil
import tempfile
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
import warnings
from collections import defaultdict
from typing import Optional, List, Tuple

import numpy as np

from ote_sdk.entities.inference_parameters import InferenceParameters
from ote_sdk.entities.metrics import (CurveMetric,
                                      LineChartInfo,
                                      MetricsGroup,
                                      Performance,
                                      ScoreMetric)
from ote_sdk.entities.shapes.box import Box
from ote_sdk.entities.train_parameters import TrainParameters
from ote_sdk.entities.label import ScoredLabel

from sc_sdk.configuration import cfg_helper, ModelConfig
from sc_sdk.configuration.helper.utils import ids_to_strings
from sc_sdk.entities.annotation import Annotation
from sc_sdk.entities.datasets import Dataset, Subset
from sc_sdk.entities.optimized_model import OptimizedModel, ModelPrecision
from sc_sdk.entities.task_environment import TaskEnvironment


from sc_sdk.entities.model import Model, ModelStatus, NullModel

from sc_sdk.entities.resultset import ResultSet, ResultsetPurpose
from sc_sdk.usecases.evaluation.metrics_helper import MetricsHelper
from sc_sdk.usecases.reporting.time_monitor_callback import TimeMonitorCallback
from sc_sdk.usecases.tasks.interfaces.evaluate_interface import IEvaluationTask
from sc_sdk.usecases.tasks.interfaces.training_interface import ITrainingTask
from sc_sdk.usecases.tasks.interfaces.inference_interface import IInferenceTask
from sc_sdk.usecases.tasks.interfaces.optimization_interface import IOptimizationTask, OptimizationType
from sc_sdk.usecases.tasks.interfaces.export_interface import IExportTask, ExportType
from sc_sdk.usecases.tasks.interfaces.unload_interface import IUnload
from sc_sdk.logging import logger_factory

from mmcv.parallel import MMDistributedDataParallel
from mmcv.runner import load_checkpoint, get_dist_info, init_dist, master_only
from mmcv.utils import Config
from mmdet.apis import train_detector, single_gpu_test, multi_gpu_test, export_model
from mmdet.apis.ote.apis.detection.configuration import OTEDetectionConfig
from mmdet.apis.ote.apis.detection.config_utils import (patch_config, set_hyperparams, prepare_for_training,
    prepare_for_testing)
from mmdet.apis.ote.extension.utils.hooks import OTELoggerHook
from mmdet.datasets import build_dataset, build_dataloader
from mmdet.models import build_detector
from mmdet.parallel import MMDataCPU


logger = logger_factory.get_logger("OTEDetectionTask")


def init_dist_cpu(launcher, backend, **kwargs):
    if mp.get_start_method(allow_none=True) is None:
        mp.set_start_method('spawn')
    if launcher == 'pytorch':
        dist.init_process_group(backend=backend, **kwargs)
    else:
        raise ValueError(f'Invalid launcher type: {launcher}')


class OTEDetectionTask(ITrainingTask, IInferenceTask, IExportTask, IEvaluationTask, IUnload):

    task_environment: TaskEnvironment

    def __init__(self, task_environment: TaskEnvironment):
        """"
        Task for training object detection models using OTEDetection.

        """
        logger.info(f"Loading OTEDetectionTask.")

        self.task_environment = task_environment
        self.hyperparams = hyperparams = task_environment.get_hyper_parameters(OTEDetectionConfig)

        self.scratch_space = tempfile.mkdtemp(prefix="ote-det-scratch-")
        logger.info(f"Scratch space created at {self.scratch_space}")
        self.labels = task_environment.get_labels(False)

        if not torch.distributed.is_initialized():
            os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
            os.environ.setdefault("MASTER_PORT", "29500")
            os.environ.setdefault("WORLD_SIZE", "1")
            os.environ.setdefault("RANK", "0")
            if torch.cuda.is_available():
                init_dist(launcher='pytorch')
            else:
                init_dist_cpu(launcher='pytorch', backend="gloo")
        self.rank, self.world_size = get_dist_info()
        self.gpu_ids = range(self.world_size)
        logger.warning(f'World size {self.world_size}, rank {self.rank}')

        template_file_path = task_environment.model_template.model_template_path

        # Get and prepare mmdet config.
        base_dir = os.path.abspath(os.path.dirname(template_file_path))
        config_file_path = os.path.join(base_dir, hyperparams.algo_backend.model)
        self.config = Config.fromfile(config_file_path)
        patch_config(self.config, self.scratch_space, self.labels, random_seed=42)
        set_hyperparams(self.config, hyperparams)
        self.config.gpu_ids = self.gpu_ids

        # Create and initialize PyTorch model.
        self.model = self._load_model(task_environment.model)

        # Extra control variables.
        self.training_round_id = 0
        self.training_work_dir = None
        self.is_training = False
        self.should_stop = False
        self.time_monitor = None


    def _load_model(self, model: Model):
        if model != NullModel():
            # If a model has been trained and saved for the task already, create empty model and load weights here
            buffer = io.BytesIO(model.get_data("weights.pth"))
            model_data = torch.load(buffer, map_location=torch.device('cpu'))

            model = self._create_model(self.config, from_scratch=True)

            try:
                model.load_state_dict(model_data['model'])
                logger.info(f"Loaded model weights from Task Environment")
            except BaseException as ex:
                raise ValueError("Could not load the saved model. The model file structure is invalid.") \
                    from ex
        else:
            # If there is no trained model yet, create model with pretrained weights as defined in the model config
            # file.
            model = self._create_model(self.config, from_scratch=False)
            logger.info(f"No trained model in project yet. Created new model with general-purpose pretrained weights.")
        return model

    @staticmethod
    def _create_model(config: Config, from_scratch: bool = False):
        """
        Creates a model, based on the configuration in config

        :param config: mmdetection configuration from which the model has to be built
        :param from_scratch: bool, if True does not load any weights

        :return model: Model in training mode
        """
        model_cfg = copy.deepcopy(config.model)

        init_from = None if from_scratch else config.get('load_from', None)
        logger.warning(init_from)
        if init_from is not None:
            # No need to initialize backbone separately, if all weights are provided.
            model_cfg.pretrained = None
            logger.warning('build detector')
            model = build_detector(model_cfg)
            # Load all weights.
            logger.warning('load checkpoint')
            load_checkpoint(model, init_from, map_location='cpu')
        else:
            logger.warning('build detector')
            model = build_detector(model_cfg)
        return model


    def infer(self, dataset: Dataset, inference_parameters: Optional[InferenceParameters] = None) -> Dataset:
        """ Analyzes a dataset using the latest inference model. """
        set_hyperparams(self.config, self.hyperparams)

        is_evaluation = inference_parameters is not None and inference_parameters.is_evaluation
        confidence_threshold = self._get_confidence_threshold(is_evaluation)
        logger.info(f'Confidence threshold {confidence_threshold}')

        prediction_results, _ = self._infer_detector(self.model, self.config, dataset, False)

        if self.rank == 0:
            # Loop over dataset again to assign predictions. Convert from MMDetection format to OTE format
            for dataset_item, output in zip(dataset, prediction_results):
                width = dataset_item.width
                height = dataset_item.height

                shapes = []
                for label_idx, detections in enumerate(output):
                    for i in range(detections.shape[0]):
                        probability = float(detections[i, 4])
                        coords = detections[i, :4].astype(float).copy()
                        coords /= np.array([width, height, width, height], dtype=float)
                        coords = np.clip(coords, 0, 1)

                        if probability < confidence_threshold:
                            continue

                        assigned_label = [ScoredLabel(self.labels[label_idx], probability=probability)]
                        if coords[3] - coords[1] <= 0 or coords[2] - coords[0] <= 0:
                            continue

                        shapes.append(Annotation(
                            Box(x1=coords[0], y1=coords[1], x2=coords[2], y2=coords[3]),
                            labels=assigned_label))

                dataset_item.append_annotations(shapes)

        return dataset


    def _infer_detector(self, model: torch.nn.Module, config: Config, dataset: Dataset,
                        eval: Optional[bool] = False, metric_name: Optional[str] = 'mAP') -> Tuple[List, float]:
        model.eval()
        test_config = prepare_for_testing(config, dataset)
        mm_val_dataset = build_dataset(test_config.data.test)
        batch_size = 1
        mm_val_dataloader = build_dataloader(mm_val_dataset,
                                             samples_per_gpu=batch_size,
                                             workers_per_gpu=test_config.data.workers_per_gpu,
                                             num_gpus=1,
                                             dist=True,
                                             shuffle=False)

        if torch.cuda.is_available():
            model = MMDistributedDataParallel(
                model.cuda(),
                device_ids=[torch.cuda.current_device()],
                broadcast_buffers=False)
            eval_predictions = multi_gpu_test(model, mm_val_dataloader)
        else:
            model = MMDataCPU(model)
            eval_predictions = single_gpu_test(model, mm_val_dataloader, show=False)

        metric = None
        if eval and self.rank == 0:
            metric = mm_val_dataset.evaluate(eval_predictions, metric=metric_name)[metric_name]
        return eval_predictions, metric


    @master_only
    def evaluate(self,
                 output_result_set: ResultSet,
                 evaluation_metric: Optional[str] = None):
        """ Computes performance on a resultset """
        params = self.hyperparams

        result_based_confidence_threshold = params.postprocessing.result_based_confidence_threshold

        logger.info('Computing F-measure' + (' with auto threshold adjustment' if result_based_confidence_threshold else ''))
        f_measure_metrics = MetricsHelper.compute_f_measure(output_result_set,
                                                            result_based_confidence_threshold,
                                                            False,
                                                            False)

        if output_result_set.purpose is ResultsetPurpose.EVALUATION:
            # only set configurable params based on validation result set
            if result_based_confidence_threshold:
                best_confidence_threshold = f_measure_metrics.best_confidence_threshold.value
                if best_confidence_threshold is not None:
                    logger.info(f"Setting confidence_threshold to " f"{best_confidence_threshold} based on results")
                    # params.postprocessing.confidence_threshold = best_confidence_threshold
                else:
                    raise ValueError(f"Cannot compute metrics: Invalid confidence threshold!")

            # self.task_environment.set_configurable_parameters(params)
        logger.info(f"F-measure after evaluation: {f_measure_metrics.f_measure.value}")
        return f_measure_metrics.get_performance()


    def train(self, dataset: Dataset, output_model: Model, train_parameters: Optional[TrainParameters] = None):
        """ Trains a model on a dataset """

        set_hyperparams(self.config, self.hyperparams)
        self.training_round_id += 1

        train_dataset = dataset.get_subset(Subset.TRAINING)
        val_dataset = dataset.get_subset(Subset.VALIDATION)
        config = self.config

        # Create new model if training from scratch.
        old_model = copy.deepcopy(self.model)
        # if train_parameters is not None and train_parameters.train_on_empty_model:
        #     logger.info("Training from scratch, creating new model")
        #     # FIXME. Isn't it an overkill? Consider calling init_weights instead.
        #     self.model = self._create_model(config=config, from_scratch=True)

        # Evaluate model performance before training.
        _, initial_performance = self._infer_detector(self.model, config, val_dataset, True)

        # Check for stop signal between pre-eval and training. If training is cancelled at this point,
        # old_model should be restored.
        if self.should_stop:
            logger.info('Training cancelled.')
            self.model = old_model
            self.should_stop = False
            self.is_training = False
            self.training_work_dir = None
            self.time_monitor = None
            self.training_work_dir = None
            return

        # Run training.
        self.time_monitor = TimeMonitorCallback(0, 0, 0, 0, update_progress_callback=lambda _: None)
        learning_curves = defaultdict(OTELoggerHook.Curve)
        training_config = prepare_for_training(config, train_dataset, val_dataset,
                                               self.training_round_id, self.time_monitor, learning_curves)
        self.training_work_dir = training_config.work_dir
        mm_train_dataset = build_dataset(training_config.data.train)
        self.is_training = True
        self.model.train()
        train_detector(model=self.model, dataset=mm_train_dataset, cfg=training_config, distributed=True, validate=True)

        # Check for stop signal when training has stopped. If should_stop is true, training was cancelled and no new
        # model should be returned. Old train model is restored.
        if self.should_stop:
            logger.info('Training cancelled.')
            self.model = old_model
            self.should_stop = False
            self.is_training = False
            self.training_work_dir = None
            self.time_monitor = None
            return

        # Evaluate model performance after training.
        _, final_performance = self._infer_detector(self.model, config, val_dataset, True)

        if self.rank == 0:
            improved = final_performance > initial_performance

            # Return a new model if model has improved, or there is no model yet.
            if improved or isinstance(self.task_environment.model, NullModel):
                if improved:
                    logger.info("Training finished, and it has an improved model")
                else:
                    logger.info("First training round, saving the model.")
                # Add mAP metric and loss curves
                training_metrics = self._generate_training_metrics_group(learning_curves)
                performance = Performance(score=ScoreMetric(value=final_performance, name="mAP"),
                                          dashboard_metrics=training_metrics)
                logger.info('FINAL MODEL PERFORMANCE\n' + str(performance))
                self.save_model(output_model)
                output_model.performance = performance
                output_model.model_status = ModelStatus.SUCCESS
            else:
                logger.info("Model performance has not improved while training. No new model has been saved.")
                # Restore old training model if training from scratch and not improved
                self.model = old_model

        self.is_training = False
        self.training_work_dir = None
        self.time_monitor = None


    @master_only
    def save_model(self, output_model: Model):
        buffer = io.BytesIO()
        hyperparams = self.task_environment.get_hyper_parameters(OTEDetectionConfig)
        hyperparams_str = ids_to_strings(cfg_helper.convert(hyperparams, dict, enum_to_str=True))
        labels = {label.name: label.color.rgb_tuple for label in self.labels}
        modelinfo = {'model': self.model.state_dict(), 'config': hyperparams_str, 'labels': labels, 'VERSION': 1}
        torch.save(modelinfo, buffer)
        output_model.set_data("weights.pth", buffer.getvalue())


    def get_training_progress(self) -> float:
        """
        Calculate the progress of the current training

        :return: training progress in percent
        """
        if self.time_monitor is not None:
            return self.time_monitor.get_progress()
        return -1.0


    @master_only
    def cancel_training(self):
        """
        Sends a cancel training signal to gracefully stop the optimizer. The signal consists of creating a
        '.stop_training' file in the current work_dir. The runner checks for this file periodically.
        The stopping mechanism allows stopping after each iteration, but validation will still be carried out. Stopping
        will therefore take some time.
        """
        logger.info("Cancel training requested.")
        self.should_stop = True
        stop_training_filepath = os.path.join(self.training_work_dir, '.stop_training')
        open(stop_training_filepath, 'a').close()


    def _generate_training_metrics_group(self, learning_curves) -> Optional[List[MetricsGroup]]:
        """
        Parses the mmdetection logs to get metrics from the latest training run

        :return output List[MetricsGroup]
        """
        output: List[MetricsGroup] = []

        # Learning curves
        for key, curve in learning_curves.items():
            metric_curve = CurveMetric(xs=curve.x, ys=curve.y, name=key)
            visualization_info = LineChartInfo(name=key, x_axis_label="Epoch", y_axis_label=key)
            output.append(MetricsGroup(metrics=[metric_curve], visualization_info=visualization_info))

        return output


    def _get_confidence_threshold(self, is_evaluation: bool) -> float:
        """
        Retrieves the threshold for confidence from the configurable parameters. If
        is_evaluation is True, the confidence threshold is set to 0 in order to compute optimum values
        for the thresholds.

        :param is_evaluation: bool, True in case analysis is requested for evaluation

        :return confidence_threshold: float, threshold for prediction confidence
        """

        hyperparams = self.hyperparams
        confidence_threshold = hyperparams.postprocessing.confidence_threshold
        result_based_confidence_threshold = hyperparams.postprocessing.result_based_confidence_threshold
        if is_evaluation:
            if result_based_confidence_threshold:
                confidence_threshold = 0.0
        return confidence_threshold

    @staticmethod
    def _is_docker():
        """
        Checks whether the task runs in docker container

        :return bool: True if task runs in docker
        """
        path = '/proc/self/cgroup'
        is_in_docker = False
        if os.path.isfile(path):
            with open(path) as f:
                is_in_docker = is_in_docker or any('docker' in line for line in f)
        is_in_docker = is_in_docker or os.path.exists('/.dockerenv')
        return is_in_docker


    def unload(self):
        """
        Unload the task
        """
        self._delete_scratch_space()
        if self._is_docker():
            logger.warning(
                "Got unload request. Unloading models. Throwing Segmentation Fault on purpose")
            import ctypes
            ctypes.string_at(0)
        else:
            logger.warning("Got unload request, but not on Docker. Only clearing CUDA cache")
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                logger.warning(f"CUDA cache is cleared. "
                    "Torch is still occupying {torch.cuda.memory_allocated()} bytes of GPU memory")
            logger.warning("Done unloading.")


    @master_only
    def export(self,
               export_type: ExportType,
               output_model: OptimizedModel):
        assert export_type == ExportType.OPENVINO
        optimized_model_precision = ModelPrecision.FP32
        with tempfile.TemporaryDirectory(prefix="ote-det-export-") as tempdir:
            logger.info(f'Optimized model will be temporarily saved to "{tempdir}"')
            try:
                from torch.jit._trace import TracerWarning
                warnings.filterwarnings("ignore", category=TracerWarning)
                if torch.cuda.is_available():
                    model = self.model.cuda(self.config.gpu_ids[0])
                else:
                    model = self.model.cpu()
                export_model(model, self.config, tempdir,
                             target='openvino', precision=optimized_model_precision.name)
                bin_file = [f for f in os.listdir(tempdir) if f.endswith('.bin')][0]
                xml_file = [f for f in os.listdir(tempdir) if f.endswith('.xml')][0]
                with open(os.path.join(tempdir, bin_file), "rb") as f:
                    output_model.set_data("openvino.bin", f.read())
                with open(os.path.join(tempdir, xml_file), "rb") as f:
                    output_model.set_data("openvino.xml", f.read())
                output_model.precision = [optimized_model_precision]
            except Exception as ex:
                raise RuntimeError("Optimization was unsuccessful.") from ex


    @master_only
    def _delete_scratch_space(self):
        """
        Remove model checkpoints and mmdet logs
        """
        if os.path.exists(self.scratch_space):
            shutil.rmtree(self.scratch_space, ignore_errors=False)
