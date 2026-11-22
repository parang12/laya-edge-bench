"""Write model validation and MQTT benchmark results to an Excel workbook."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


def _append(sheet, values):
    """Keep imported strings as literal cell values, including leading '='."""
    sheet.append(list(values))
    for cell in sheet[sheet.max_row]:
        if cell.data_type == "f":
            cell.data_type = "s"


def _table(sheet, columns, rows):
    sheet.freeze_panes = "A2"
    _append(sheet, (label for _, label in columns))
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="11776A")
        cell.alignment = Alignment(vertical="center")
    for row in rows:
        _append(sheet, (row.get(key) for key, _ in columns))
    sheet.auto_filter.ref = sheet.dimensions
    sheet.row_dimensions[1].height = 25
    for index, (_, label) in enumerate(columns, 1):
        values = (sheet.cell(row, index).value for row in range(2, min(sheet.max_row, 102) + 1))
        width = min(48, max(14, len(label) * 2, *(len(str(value)) + 2 for value in values if value is not None)))
        sheet.column_dimensions[get_column_letter(index)].width = width


def export_report(path, model_info=None, validation=None, mqtt=None):
    """Save current and partial results to the selected Excel file.

    Validation contains status, rows and optional metrics. MQTT contains
    status, rows and optional summary. Missing results become empty sheets.
    """
    model_info = model_info or {}
    validation = validation or {}
    mqtt = mqtt or {}
    metrics = validation.get("metrics") or {}
    summary = mqtt.get("summary") or {}
    gateway = mqtt.get("gateway") or {}
    validation_rows = validation.get("rows") or []
    mqtt_rows = mqtt.get("rows") or []

    book = Workbook()
    overview = book.active
    overview.title = "요약"
    entries = [
        ("저장 시각", datetime.now().astimezone().isoformat(timespec="seconds")),
        ("모델 경로", model_info.get("path")),
        ("모델 불러오기 시간 (ms)", model_info.get("load_ms")),
        ("시험 데이터 출처", model_info.get("dataset_description")),
        ("모델 검증 상태", validation.get("status") or "미실행"),
        ("모델 검증 기록 수", len(validation_rows)),
        ("모델 검증 정확도", metrics.get("accuracy")),
        ("모델 검증 Macro-F1", metrics.get("macro_f1")),
        ("MQTT 테스트 상태", mqtt.get("status") or "미실행"),
        ("MQTT 테스트 기록 수", len(mqtt_rows)),
        ("MQTT 정확도", summary.get("accuracy")),
        ("MQTT 정확도 기준 건수", summary.get("accuracy_n")),
        ("MQTT 응답 성공 건수", summary.get("success_count")),
        ("MQTT 시간 초과 건수", summary.get("timeout_count")),
        ("MQTT 오류 건수", summary.get("error_count")),
        ("MQTT 전체 응답 시간 중앙값 (ms)", summary.get("median_ms")),
        ("MQTT 전체 응답 시간 p95 (ms)", summary.get("p95_ms")),
        ("시간 측정 범위", "모델 입력·출력 시각은 추론 호출 직전과 결과 반환 직후의 UTC 시각입니다. 모델 판단 시간은 그 사이의 경과 시간입니다. 전체 응답 시간은 MQTT 요청 전송부터 응답 수신까지입니다."),
        ("반복 사례", "MQTT 반복 20건은 시간 통계에 포함하며, 고유 사례 정확도에는 포함하지 않습니다."),
        ("MQTT 실행 위치", mqtt.get("target")),
        ("MQTT 실행 모델", mqtt.get("model")),
        ("MQTT 브로커 주소", mqtt.get("broker")),
        ("MQTT Macro-F1", summary.get("macro_f1")),
        ("MQTT 모델 추론 중앙값 (ms)", summary.get("inference_median_ms")),
        ("MQTT 모델 추론 p95 (ms)", summary.get("inference_p95_ms")),
        ("게이트웨이 CPU 한도 (코어)", gateway.get("cpu_limit")),
        ("게이트웨이 메모리 한도 (MiB)", gateway.get("memory_limit_mib")),
        ("게이트웨이 최대 메모리 (MiB)", gateway.get("memory_peak_mib")),
        ("게이트웨이 추론 스레드", gateway.get("torch_threads")),
        ("게이트웨이 예열 횟수", gateway.get("warmup_count")),
        ("게이트웨이 입력 특징 설정", gateway.get("features")),
        ("게이트웨이 Laya 버전", gateway.get("laya_version")),
        ("게이트웨이 PyTorch 버전", gateway.get("torch_version")),
    ]
    _table(overview, (("name", "항목"), ("value", "값")),
           ({"name": name, "value": value} for name, value in entries))
    overview.column_dimensions["A"].width = 35
    overview.column_dimensions["B"].width = 90
    for row in overview.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    percent_labels = {"모델 검증 정확도", "모델 검증 Macro-F1", "MQTT 정확도", "MQTT Macro-F1"}
    for label, value in overview.iter_rows(min_row=2):
        if label.value in percent_labels:
            value.number_format = "0.00%"

    class_columns = (
        ("label", "실제 유형"), ("correct", "맞힌 수"), ("total", "전체"),
        ("precision", "정밀도"), ("recall", "재현율"), ("f1", "F1"),
    )
    for sheet_title, scores in (("유형별 성능", metrics), ("MQTT 유형별 성능", mqtt.get("metrics") or {})):
        classes = book.create_sheet(sheet_title)
        _table(classes, class_columns, scores.get("classes") or [])
        for row in classes.iter_rows(min_row=2, min_col=4, max_col=6):
            for cell in row:
                cell.number_format = "0.00%"

    validation_sheet = book.create_sheet("모델 검증")
    model_timing_columns = (
        ("model_input_at", "모델 입력 시각 (UTC)"),
        ("model_output_at", "모델 출력 시각 (UTC)"),
        ("inference_ms", "모델 판단 시간 (ms)"),
    )
    _table(validation_sheet, (
        ("udi", "데이터 UDI"), ("expected", "정답"),
        ("prediction", "모델 판단"), ("status", "처리 상태"),
        *model_timing_columns,
    ), validation_rows)

    mqtt_sheet = book.create_sheet("MQTT 테스트")
    _table(mqtt_sheet, (
        ("seq", "요청 번호"), ("udi", "데이터 UDI"), ("expected", "정답"),
        ("prediction", "모델 판단"), ("repeated", "반복 사례"),
        ("status", "처리 상태"), *model_timing_columns,
        ("total_ms", "전체 응답 시간 (ms)"), ("error", "오류 상세"),
    ), mqtt_rows)

    destination = Path(path)
    temporary = None
    try:
        with NamedTemporaryFile(prefix=".model-bench-", suffix=".xlsx",
                                dir=destination.parent, delete=False) as file:
            temporary = Path(file.name)
        book.save(temporary)
        os.replace(temporary, destination)
    finally:
        book.close()
        if temporary is not None and temporary.exists():
            temporary.unlink()
