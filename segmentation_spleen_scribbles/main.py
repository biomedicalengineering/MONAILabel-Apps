# Copyright 2020 - 2021 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
from distutils.util import strtobool
from typing import Dict

from monai.apps import load_from_mmar
from monailabel.interfaces.app import MONAILabelApp
from monailabel.interfaces.tasks.infer import InferTask, InferType
from monailabel.interfaces.tasks.scoring import ScoringMethod
from monailabel.interfaces.tasks.strategy import Strategy
from monailabel.interfaces.tasks.train import TrainTask
from monailabel.scribbles.infer import HistogramBasedGraphCut
from monailabel.tasks.activelearning.random import Random
from monailabel.tasks.activelearning.tta import TTA
from monailabel.tasks.scoring.tta import TTAScoring

from lib import (MyTrain, SegmentationWithWriteLogits, SpleenISegCRF,
                 SpleenISegGraphCut, SpleenISegSimpleCRF, SpleenMIDeepSeg)
from lib.activelearning import MyStrategy


logger = logging.getLogger(__name__)


class MyApp(MONAILabelApp):
    def __init__(self, app_dir, studies, conf):
        self.model_dir = os.path.join(app_dir, "model")
        self.final_model = os.path.join(self.model_dir, "model.pt")

        self.mmar = "clara_pt_spleen_ct_segmentation_1"

        self.tta_enabled = strtobool(conf.get("tta_enabled", "false"))
        self.tta_samples = int(conf.get("tta_samples", "5"))

        logger.info(
            f"TTA Enabled: {self.tta_enabled}; Samples: {self.tta_samples}")

        super().__init__(
            app_dir=app_dir,
            studies=studies,
            conf=conf,
            name="Segmentation - Spleen + Scribbles",
            description="Active Learning solution to label Spleen Organ over 3D CT Images.  "
            "It includes multiple scribbles method that incorporate user scribbles to improve labels",
        )

    def init_infers(self) -> Dict[str, InferTask]:
        infers = {
            "Spleen_Segmentation": SegmentationWithWriteLogits(
                self.final_model, load_from_mmar(self.mmar, self.model_dir)
            ),
            "Histogram+GraphCut": HistogramBasedGraphCut(),
            "MIDeepSeg": SpleenMIDeepSeg(),
            "ISeg+GraphCut": SpleenISegGraphCut(),
            "ISeg+MONAICRF": SpleenISegCRF(),
            "ISeg+SimpleCRF": SpleenISegSimpleCRF(),
        }

        # Simple way to Add deepgrow 2D+3D models for infer tasks
        infers.update(self.deepgrow_infer_tasks(self.model_dir))
        return infers

    def init_trainers(self) -> Dict[str, TrainTask]:
        return {
            "segmentation_spleen": MyTrain(
                self.model_dir, load_from_mmar(self.mmar, self.model_dir), publish_path=self.final_model
            )
        }

    def init_strategies(self) -> Dict[str, Strategy]:
        strategies: Dict[str, Strategy] = {}
        if self.tta_enabled:
            strategies["TTA"] = TTA()
        strategies["random"] = Random()
        strategies["first"] = MyStrategy()
        return strategies

    def init_scoring_methods(self) -> Dict[str, ScoringMethod]:
        methods: Dict[str, ScoringMethod] = {}
        if self.tta_enabled:
            methods["TTA"] = TTAScoring(
                model=self.final_model,
                network=load_from_mmar(self.mmar, self.model_dir),
                num_samples=self.tta_samples,
                spatial_size=(128, 128, 64),
                spacing=(1.0, 1.0, 1.0),
            )
        return methods

    def infer(self, request, datastore=None):
        image = request.get("image")

        # add saved logits into request
        if self._infers[request.get("model")].type == InferType.SCRIBBLES:
            saved_labels = self.datastore().get_labels_by_image_id(image)
            for tag, label in saved_labels.items():
                if tag == "logits":
                    request["logits"] = self.datastore(
                    ).get_label_uri(label, tag)
            logger.info(f"Updated request: {request}")

        result = super().infer(request)
        result_params = result.get("params")

        # save logits
        logits = result_params.get("logits")
        if logits and self._infers[request.get("model")].type == InferType.SEGMENTATION:
            self.datastore().save_label(image, logits, "logits", {})
            os.unlink(logits)

        result_params.pop("logits", None)
        logger.info(f"Final Result: {result}")
        return result
