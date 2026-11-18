"""AI4I v7 demonstration. Independent from conveyor rules and physical controls."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from threading import Lock, Thread
from time import perf_counter_ns


DEFAULT_MODEL = Path.home() / "Documents" / "3학년2학기" / "사물인테넷" / "model" / "laya_ai4i_v7"
OVERSTRAIN_LIMIT = {"L": 11000, "M": 12000, "H": 13000}


def build_state(values, *, features="v7"):
    if not isinstance(values, dict):
        raise ValueError("AI4I 센서 입력이 필요합니다.")
    product = values.get("product_type")
    if product not in ("L", "M", "H"):
        raise ValueError("제품 유형은 L, M, H 중 하나여야 합니다.")

    def number(key, label, integer=False):
        value = values.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{label}은 0 이상의 유한한 숫자여야 합니다.")
        if integer and int(value) != value:
            raise ValueError(f"{label}은 정수여야 합니다.")
        return int(value) if integer else float(value)

    air = number("air_temperature_K", "공기 온도")
    process = number("process_temperature_K", "공정 온도")
    rpm = number("rotational_speed_rpm", "회전 속도", True)
    torque = number("torque_Nm", "토크")
    wear = number("tool_wear_min", "공구 마모 시간", True)
    try:
        gap = process - air
        power = torque * rpm * 2 * math.pi / 60
        wear_torque = wear * torque
        state = {"product_type": product, "air_temperature_K": air, "process_temperature_K": process,
                 "rotational_speed_rpm": rpm, "torque_Nm": torque, "tool_wear_min": wear,
                 "temperature_gap_K": round(gap, 2), "power_W": round(power, 2),
                 "wear_torque": round(wear_torque, 2)}
        if features == "margin":
            limit = OVERSTRAIN_LIMIT[product]
            # Match feats() in the margin sweep notebook, including field order.
            state.update({
                "overstrain_limit_minNm": limit,
                "gap_minus_8.6K": round(gap - 8.6, 2),
                "rpm_minus_1380": rpm - 1380,
                "power_minus_3500W": round(power - 3500, 1),
                "9000W_minus_power": round(9000 - power, 1),
                "overstrain_limit_minus_wear_torque": round(limit - wear_torque, 1),
                "tool_wear_minus_200min": wear - 200,
            })
        if any(type(v) is float and not math.isfinite(v) for v in state.values()):
            raise ValueError("계산된 특징값이 허용 범위를 벗어났습니다.")
    except OverflowError as exc:
        raise ValueError("입력 값이 너무 큽니다.") from exc
    return state


class AI4ITrial:
    def __init__(self, model_path=None):
        self.model_path = Path(model_path or os.environ.get("LAYA_AI4I_MODEL", DEFAULT_MODEL))
        self.lock = Lock()
        self.model_lock = Lock()
        self.model = None
        self.questions = None
        self.features = "v7"
        self.status = {"phase": "idle", "message": "AI4I 입력을 선택하고 모델 판단을 실행하세요."}

    def snapshot(self):
        with self.lock:
            return dict(self.status)

    def submit(self, values):
        state = build_state(values)
        with self.lock:
            if self.status["phase"] == "working":
                raise ValueError("이전 모델 판단이 진행 중입니다.")
            self.status = {"phase": "working", "message": "모델을 불러오고 판단 중입니다. 첫 실행은 시간이 걸릴 수 있습니다."}
        Thread(target=self._run, args=(state,), daemon=True).start()
        return self.snapshot()

    def load_model(self):
        with self.model_lock:
            self._load_model()

    def _load_model(self):
        if self.model is None:
            if not (self.model_path / "model.safetensors").is_file():
                raise FileNotFoundError(f"모델 가중치를 찾을 수 없습니다: {self.model_path}")
            with (self.model_path / "questions.json").open(encoding="utf-8") as source:
                questions = json.load(source)
            config_path = self.model_path / "run_config.json"
            features = "v7"
            if config_path.is_file():
                with config_path.open(encoding="utf-8") as source:
                    features = json.load(source).get("features", features)
            if features not in ("v7", "margin"):
                raise ValueError(f"지원하지 않는 AI4I 입력 형식입니다. {features}")
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            import laya
            self.model = laya.load(str(self.model_path))
            self.questions = questions
            self.features = features

    def predict(self, state, cancel=None, *, allow_invalid=False, timing=None):
        with self.model_lock:
            if cancel is not None and cancel.is_set():
                return None
            self._load_model()
            payload = state if self.features == "margin" else json.dumps(state, ensure_ascii=False)
            if timing is not None:
                entered_at = datetime.now(timezone.utc)
                started = perf_counter_ns()
            result = self.model.predict(payload, self.questions)
            if timing is not None:
                elapsed_ms = (perf_counter_ns() - started) / 1_000_000
                returned_at = datetime.now(timezone.utc)
                timing.update(
                    model_input_at=entered_at.isoformat(timespec="milliseconds"),
                    model_output_at=returned_at.isoformat(timespec="milliseconds"),
                    inference_ms=round(elapsed_ms, 3),
                )
            try:
                answer = result["answers"]["diagnosis"]["choice"]
            except (KeyError, TypeError):
                if allow_invalid:
                    return None
                raise
            if not allow_invalid and answer not in self.questions["diagnosis"]["criteria"]:
                raise ValueError(f"모델이 선택지 밖 답변을 반환했습니다: {answer}")
            return answer

    def _run(self, state):
        try:
            answer = self.predict(state)
            snapshot = {"phase": "done", "message": "AI4I 모델 판단 완료", "diagnosis": answer, "state": state}
        except Exception as exc:
            snapshot = {"phase": "error", "message": f"모델 실행 오류 ({type(exc).__name__}): {exc}"}
        with self.lock:
            self.status = snapshot
