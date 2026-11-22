"""Coordinate model loading and evaluations for the desktop application."""
from pathlib import Path
from threading import Event, Lock, Thread
from time import perf_counter
import json
import os

from ai4i_evaluation import load_trial, predict_sensors, score
from export_report import export_report
from mqtt_bench import DEFAULT_DOCKER_PORT, latency_summary as _latency_summary, run_mqtt


class BenchController:
    def __init__(self):
        test_path = Path(__file__).with_name("ai4i_test_cases.json")
        with test_path.open(encoding="utf-8") as handle:
            dataset = json.load(handle)
        self.cases = dataset["cases"]
        self.source = dataset["source"]
        self.model_info = {"dataset_description": self.source["description"]}
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
            start = perf_counter()
            self.trial = load_trial(Path(path))
            self.model_info = {"path": str(Path(path)), "load_ms": (perf_counter() - start) * 1000,
                               "dataset_description": self.source["description"]}
            self.validation = None
            self.mqtt_result = None
            send({"event": "result", "message": "모델을 불러왔습니다.",
                  "data": self.model_info})
        self._start(task, callback)

    def _predict(self, sensors):
        return predict_sensors(self.trial, sensors, self.cancel)

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

    def mqtt(self, callback, *, external=False):
        if not external and self.trial is None:
            raise ValueError("모델을 먼저 불러오세요.")
        def task(send):
            target = "Docker 게이트웨이" if external else "PC 내부"
            result = {"status": "running", "rows": [], "summary": {}, "target": target,
                      "model": None if external else Path(self.model_info["path"]).name}
            self.mqtt_result = result
            send({"event": "status", "message": f"{target} MQTT 연결 중입니다."})

            def on_record(row):
                result["rows"].append(row)
                send({"event": "progress", "message": f"MQTT 테스트 {len(result['rows'])} / 100회",
                      "progress": len(result["rows"])})

            def on_gateway(metadata):
                result.update(gateway=metadata, model=metadata.get("model"))

            try:
                address = None
                if external:
                    port = int(os.environ.get("LAYA_MQTT_PORT", DEFAULT_DOCKER_PORT))
                    if not 1 <= port <= 65535:
                        raise ValueError("LAYA_MQTT_PORT는 1~65535 사이여야 합니다.")
                    address = ("127.0.0.1", port)
                    result["broker"] = f"{address[0]}:{address[1]}"
                run_mqtt(self.cases, None if external else self._predict,
                         on_record=on_record, stop_event=self.cancel, broker_address=address,
                         on_gateway=on_gateway if external else None)
                result["status"] = "complete" if len(result["rows"]) == 100 else "stopped"
            except Exception:
                result["status"] = "error"
                raise
            rows = result["rows"]
            successes = [r for r in rows if r["status"] == "ok"]
            unique = [r for r in rows if not r["repeated"]]
            metrics = score(unique) if len(unique) == 80 else {}
            result["metrics"] = metrics
            center, p95 = _latency_summary(successes, "total_ms")
            inference_center, inference_p95 = _latency_summary(successes, "inference_ms")
            summary = {"accuracy": metrics.get("accuracy"),
                       "macro_f1": metrics.get("macro_f1"),
                       "accuracy_n": len(unique),
                       "success_count": len(successes),
                       "timeout_count": sum(r["status"] == "timeout" for r in rows),
                       "error_count": sum(r["status"] not in ("ok", "timeout") for r in rows),
                       "median_ms": center, "p95_ms": p95,
                       "inference_median_ms": inference_center, "inference_p95_ms": inference_p95}
            result["summary"] = summary
            if result["status"] != "complete":
                message = f"MQTT 테스트 중지 · {len(rows)} / 100회"
            else:
                latency = f"{center:.1f} ms" if center is not None else "측정 없음"
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
