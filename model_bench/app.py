"""Windows desktop interface for the Laya model and MQTT benchmark."""
from __future__ import annotations

from pathlib import Path
from queue import Empty, Queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from bench_core import BenchController


class BenchApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Laya Edge · 모델 검증")
        self.geometry("970x720")
        self.minsize(800, 620)
        self.configure(background="#f3f7f6")

        self.controller = BenchController()
        self.events = Queue()
        self.loaded = False
        self.busy = False
        self.has_results = False
        self.active_action = None
        self.closing = False

        self.model_path = tk.StringVar(value="선택한 모델 폴더가 없습니다.")
        self.status = tk.StringVar(value="모델 폴더를 선택해 주세요.")
        self.progress_text = tk.StringVar(value="대기 중")
        self.summary = tk.StringVar(value="테스트가 끝나면 결과가 표시됩니다.")
        self.progress = tk.DoubleVar(value=0)

        self._configure_styles()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll_events)

    def _configure_styles(self):
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background="#f3f7f6")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f3f7f6", foreground="#263f3d",
                        font=("맑은 고딕", 10))
        style.configure("Card.TLabel", background="#ffffff", foreground="#263f3d",
                        font=("맑은 고딕", 10))
        style.configure("Heading.TLabel", background="#f3f7f6", foreground="#183d39",
                        font=("맑은 고딕", 19, "bold"))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#183d39",
                        font=("맑은 고딕", 12, "bold"))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#637d79",
                        font=("맑은 고딕", 9))
        style.configure("TButton", font=("맑은 고딕", 10), padding=(13, 8))
        style.configure("Primary.TButton", background="#087c6c", foreground="white",
                        font=("맑은 고딕", 10, "bold"), padding=(13, 8))
        style.map("Primary.TButton",
                  background=[("disabled", "#c8d8d4"), ("active", "#096a5e")],
                  foreground=[("disabled", "#71837f")])
        style.configure("Bench.Horizontal.TProgressbar", troughcolor="#e4efec",
                        background="#0b8978", borderwidth=0, thickness=12)

    def _card(self, parent):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=20)
        frame.pack(fill="x", pady=(0, 12))
        return frame

    def _build(self):
        outer = ttk.Frame(self, padding=(24, 20))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="모델 검증", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(outer, text="모델의 분류 성능과 MQTT 처리 시간을 확인합니다.").pack(
            anchor="w", pady=(4, 16))

        model = self._card(outer)
        ttk.Label(model, text="모델 선택", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(model, textvariable=self.model_path, style="Muted.TLabel",
                  wraplength=830).pack(fill="x", pady=(5, 9))
        self.select_button = ttk.Button(model, text="모델 폴더 선택",
                                        command=self._choose_model)
        self.select_button.pack(anchor="w")

        tests = self._card(outer)
        ttk.Label(tests, text="테스트 실행", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(tests, style="Muted.TLabel",
                  text="모델 검증은 시험 사례 80건을, MQTT 테스트는 100회 요청을 실행합니다.").pack(
            anchor="w", pady=(5, 10))
        row = ttk.Frame(tests, style="Card.TFrame")
        row.pack(fill="x")
        self.validation_button = ttk.Button(row, text="모델 검증 · 80건",
                                             style="Primary.TButton",
                                             command=lambda: self._start("validate"))
        self.validation_button.pack(side="left", padx=(0, 8))
        self.mqtt_button = ttk.Button(row, text="MQTT 테스트 · 100회",
                                      style="Primary.TButton",
                                      command=lambda: self._start("mqtt"))
        self.mqtt_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(row, text="중지", command=self._stop)
        self.stop_button.pack(side="left")
        ttk.Label(tests, style="Muted.TLabel",
                  text="MQTT 테스트는 PC 내부 브로커를 사용합니다.").pack(
            anchor="w", pady=(10, 0))

        result = ttk.Frame(outer, style="Card.TFrame", padding=20)
        result.pack(fill="both", expand=True)
        heading = ttk.Frame(result, style="Card.TFrame")
        heading.pack(fill="x")
        ttk.Label(heading, text="진행과 결과", style="CardTitle.TLabel").pack(side="left")
        self.export_button = ttk.Button(heading, text="엑셀로 내보내기",
                                        command=self._export)
        self.export_button.pack(side="right")
        ttk.Label(result, textvariable=self.status, style="Card.TLabel",
                  wraplength=830).pack(fill="x", pady=(8, 5))
        ttk.Progressbar(result, maximum=100, variable=self.progress,
                        style="Bench.Horizontal.TProgressbar").pack(fill="x", pady=(0, 4))
        ttk.Label(result, textvariable=self.progress_text, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(result, textvariable=self.summary, style="Card.TLabel",
                  wraplength=830, justify="left").pack(fill="x", pady=(12, 0), anchor="nw")
        self._update_buttons()

    def _update_buttons(self):
        self.select_button.configure(state="disabled" if self.busy else "normal")
        test_state = "normal" if self.loaded and not self.busy else "disabled"
        self.validation_button.configure(state=test_state)
        self.mqtt_button.configure(state=test_state)
        self.stop_button.configure(state="normal" if self.busy else "disabled")
        self.export_button.configure(state="normal" if self.has_results and not self.busy else "disabled")

    def _queue_event(self, payload):
        self.events.put(payload)

    def _start_call(self, action, call, *, loading=False):
        self.busy = True
        self.active_action = action
        if loading:
            self.loaded = False
        self.progress.set(0)
        self.progress_text.set("준비 중")
        self._update_buttons()
        try:
            call()
        except Exception as exc:
            self.busy = False
            self.status.set(f"실행할 수 없습니다. {exc}")
            self._update_buttons()

    def _choose_model(self):
        default = Path.home() / "Documents" / "3학년2학기" / "사물인테넷" / "model"
        directory = filedialog.askdirectory(
            title="Laya 모델 폴더 선택",
            initialdir=str(default if default.is_dir() else Path.home()),
            parent=self,
        )
        if not directory:
            return
        model_dir = Path(directory)
        required = ("model.safetensors", "rl_agent_config.json", "questions.json",
                    "tokenizer", "encoder")
        missing = [item for item in required if not (model_dir / item).exists()]
        if missing:
            messagebox.showerror("모델 폴더 확인", "필수 파일이 없습니다.\n" + ", ".join(missing),
                                 parent=self)
            return
        self.model_path.set(str(model_dir))
        self.status.set("모델을 불러오는 중입니다.")
        self._start_call("load", lambda: self.controller.load(directory, self._queue_event),
                         loading=True)

    def _start(self, action):
        if not self.loaded or self.busy:
            return
        description = "모델 검증" if action == "validate" else "MQTT 테스트"
        self.status.set(f"{description}을 준비하고 있습니다.")
        self.summary.set("진행 상황을 기다리고 있습니다.")
        self._start_call(action, lambda: getattr(self.controller, action)(self._queue_event))

    def _stop(self):
        if self.busy:
            try:
                self.controller.stop()
                self.status.set("중지 요청을 보냈습니다. 현재 작업이 끝나면 중지됩니다.")
            except Exception as exc:
                self.status.set(f"중지 요청을 처리하지 못했습니다. {exc}")

    def _export(self):
        path = filedialog.asksaveasfilename(
            title="결과 저장",
            defaultextension=".xlsx",
            filetypes=[("Excel 통합 문서", "*.xlsx")],
            initialfile="laya_검증결과.xlsx",
            parent=self,
        )
        if not path:
            return
        try:
            self.controller.export(path)
        except Exception as exc:
            messagebox.showerror("저장 실패", str(exc), parent=self)
            return
        self.status.set(f"저장했습니다. {path}")

    @staticmethod
    def _format_summary(data):
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            status = {"complete": "완료", "stopped": "중지", "error": "오류",
                      "running": "진행 중"}.get(data.get("status"), "확인 중")
            if "metrics" in data:
                metrics = data.get("metrics") or {}
                rows = data.get("rows") or []
                result = [
                    f"검증 기록  {len(rows)}건  ·  상태  {status}",
                ]
                if metrics:
                    result.append(
                        f"정확도  {metrics['accuracy']:.2%}    "
                        f"Macro-F1  {metrics['macro_f1']:.4f}"
                    )
                    for item in metrics.get("classes", []):
                        result.append(
                            f"{item['label']}  {item['correct']} / {item['total']}"
                            f"  ·  재현율 {item['recall']:.2%}"
                        )
                return "\n".join(result)
            if "summary" in data:
                summary = data.get("summary") or {}
                rows = data.get("rows") or []
                result = [
                    f"진행  {len(rows)} / 100회  ·  상태  {status}",
                ]
                if summary:
                    result.append(
                        f"응답 성공  {summary.get('success_count', 0)}회    "
                        f"시간 초과  {summary.get('timeout_count', 0)}회    "
                        f"오류  {summary.get('error_count', 0)}회"
                    )
                    accuracy = summary.get("accuracy")
                    if accuracy is not None:
                        result.append(
                            f"고유 80건 기준 정확도  {accuracy:.2%}  "
                            f"({summary.get('accuracy_n', 0)}건 처리)"
                        )
                    middle = summary.get("median_ms")
                    p95 = summary.get("p95_ms")
                    if middle is not None and p95 is not None:
                        result.append(f"전체 응답 시간  중앙값 {middle:.2f} ms  ·  p95 {p95:.2f} ms")
                return "\n".join(result)
            labels = {
                "accuracy": "정확도",
                "macro_f1": "Macro-F1",
                "success": "응답 성공",
                "failures": "응답 실패",
                "total": "전체",
                "median_ms": "중앙값 (ms)",
                "p95_ms": "p95 (ms)",
                "inference_median_ms": "모델 판단 중앙값 (ms)",
            }
            lines = []
            for key, value in data.items():
                if isinstance(value, (dict, list)):
                    continue
                if key in ("accuracy", "macro_f1") and isinstance(value, (int, float)):
                    value = f"{value * 100:.2f}%"
                elif isinstance(value, float):
                    value = f"{value:.2f}"
                lines.append(f"{labels.get(key, key)}  {value}")
            return "\n".join(lines) if lines else "테스트가 완료되었습니다."
        return "테스트가 완료되었습니다."

    def _poll_events(self):
        while True:
            try:
                event = self.events.get_nowait()
            except Empty:
                break
            kind = event.get("event")
            message = event.get("message")
            if message:
                self.status.set(str(message))
            progress = event.get("progress")
            if isinstance(progress, (int, float)):
                progress = max(0, min(100, progress))
                self.progress.set(progress)
                self.progress_text.set(f"{progress:.0f}%")
            if kind == "result":
                self.busy = False
                if self.active_action == "load":
                    self.loaded = True
                    self.has_results = False
                    self.summary.set("테스트가 끝나면 결과가 표시됩니다.")
                else:
                    self.has_results = True
                    self.summary.set(self._format_summary(event.get("data")))
                self._update_buttons()
            elif kind == "error":
                self.busy = False
                self.has_results = bool(
                    (self.controller.validation or {}).get("rows")
                    or (self.controller.mqtt_result or {}).get("rows")
                )
                if self.has_results:
                    self.summary.set("이전 결과 또는 중지 전 기록을 엑셀로 저장할 수 있습니다.")
                self._update_buttons()
        if not self.closing:
            self.after(100, self._poll_events)

    def _close(self):
        self.closing = True
        try:
            self.controller.stop()
        finally:
            self.destroy()


if __name__ == "__main__":
    BenchApp().mainloop()
