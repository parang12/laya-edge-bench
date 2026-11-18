"""Coordinate model loading and evaluations for the desktop application."""
from pathlib import Path
from statistics import median
from threading import Event, Lock, Thread
from time import perf_counter
import json
import math

from ai4i_trial import AI4ITrial, build_state
from ai4i_evaluation import LABELS, INVALID, score
from export_report import export_report
from mqtt_bench import run_mqtt


class BenchController:
    def __init__(self):
        test_path = Path(__file__).with_name("ai4i_test_cases.json")
        with test_path.open(encoding="utf-8") as handle:
            dataset = json.load(handle)
        self.cases = dataset["cases"]
        self.source = dataset["source"]
        self.model_info = {}
        self.trial = None
        self.validation = None
        self.mqtt_result = None
        self.running = False
        self.cancel = Event()
        self.lock = Lock()

    def _start(self, operation, callback):
        with self.lock:
            if self.running:
                raise ValueError("진행 중인 작업이 있습니다.")
            self.running = True
            self.cancel.clear()

        def work():
            terminal = []
            def notify(event):
                if event.get("event") in ("result", "error"):
                    terminal.append(event)
                else:
                    callback(event)
            try:
                operation(notify)
            except Exception as exc:
                notify({"event": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                with self.lock:
                    self.running = False
            for event in terminal:
                callback(event)

        Thread(target=work, daemon=True).start()

    def load(self, path, callback):
        def task(send):
            send({"event": "status", "message": "모델을 불러오고 있습니다."})
            trial = AI4ITrial(Path(path))
            start = perf_counter()
            trial.load_model()
            if set(trial.questions["diagnosis"]["criteria"]) != set(LABELS):
                raise ValueError("AI4I의 다섯 고장 유형을 분류하는 모델이 필요합니다.")
            self.trial = trial
            self.model_info = {"path": str(Path(path)), "load_ms": (perf_counter() - start) * 1000,
                               "dataset_description": self.source["description"]}
            self.validation = None
            self.mqtt_result = None
            send({"event": "result", "message": "모델을 불러왔습니다.",
                  "data": self.model_info})
        self._start(task, callback)

    def _predict(self, sensors):
        result = {}
        answer = self.trial.predict(
            build_state(sensors, features=self.trial.features),
            cancel=self.cancel, allow_invalid=True, timing=result,
        )
        result["prediction"] = answer if answer in LABELS else INVALID
        return result

    def validate(self, callback):
        if self.trial is None:
            raise ValueError("모델을 먼저 불러오세요.")
        def task(send):
            rows = []
            result = {"status": "running", "rows": rows, "metrics": None}
            self.validation = result
            try:
                for case in self.cases:
                    if self.cancel.is_set():
                        break
                    prediction = self._predict(case["sensors"])
                    if self.cancel.is_set():
                        break
                    rows.append({"udi": case["udi"], "expected": case["expected"],
                                 "status": "ok", **prediction})
                    send({"event": "progress", "message": f"모델 검증 {len(rows)} / {len(self.cases)}건",
                          "progress": len(rows) / len(self.cases) * 100})
                result["status"] = "complete" if len(rows) == len(self.cases) else "stopped"
                result["metrics"] = score(rows) if rows else None
            except Exception:
                result["status"] = "error"
                raise
            metrics = result["metrics"]
            message = (f"모델 검증 {len(rows)}건 완료 · 정확도 {metrics['accuracy']:.2%} · "
                       f"Macro-F1 {metrics['macro_f1']:.4f}") if metrics else "모델 검증 중지"
            send({"event": "result", "message": message, "data": result})
        self._start(task, callback)

    def mqtt(self, callback):
        if self.trial is None:
            raise ValueError("모델을 먼저 불러오세요.")
        def task(send):
            result = {"status": "running", "rows": [], "summary": {}}
            self.mqtt_result = result
            send({"event": "status", "message": "로컬 MQTT 연결 중입니다."})

            def on_record(row):
                result["rows"].append(row)
                send({"event": "progress", "message": f"MQTT 테스트 {len(result['rows'])} / 100회",
                      "progress": len(result["rows"])})

            try:
                run_mqtt(self.cases, self._predict, on_record=on_record, stop_event=self.cancel)
                result["status"] = "complete" if len(result["rows"]) == 100 else "stopped"
            except Exception:
                result["status"] = "error"
                raise
            rows = result["rows"]
            successes = [r for r in rows if r["status"] == "ok"]
            times = sorted(r["total_ms"] for r in successes if r["total_ms"] is not None)
            unique = [r for r in rows if not r["repeated"]]
            summary = {"accuracy": sum(r["status"] == "ok" and r["prediction"] == r["expected"] for r in unique) / 80 if len(unique) == 80 else None,
                       "accuracy_n": len(unique),
                       "success_count": len(successes),
                       "timeout_count": sum(r["status"] == "timeout" for r in rows),
                       "error_count": sum(r["status"] not in ("ok", "timeout") for r in rows),
                       "median_ms": median(times) if times else None,
                       "p95_ms": times[math.ceil(.95 * len(times))-1] if times else None}
            result["summary"] = summary
            if result["status"] != "complete":
                message = f"MQTT 테스트 중지 · {len(rows)} / 100회"
            else:
                latency = f"{summary['median_ms']:.1f} ms" if times else "측정 없음"
                message = f"MQTT {len(rows)}회 · 응답 성공 {len(successes)}회 · 중앙값 {latency}"
            send({"event": "result", "message": message, "data": result})
        self._start(task, callback)

    def stop(self):
        self.cancel.set()

    def export(self, path):
        if self.running:
            raise ValueError("진행 중인 작업을 마친 뒤 내보내세요.")
        if self.validation is None and self.mqtt_result is None:
            raise ValueError("먼저 테스트를 실행하세요.")
        export_report(path, self.model_info, self.validation, self.mqtt_result)
