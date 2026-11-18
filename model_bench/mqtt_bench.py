"""Run a request/response MQTT benchmark on a local broker.

`run_mqtt(cases, predict, on_record=None, stop_event=None, timeout=10.0)`
expects 80 dictionaries with udi, expected and sensors, and a callable that
accepts a sensors dictionary and returns a classification string or a
dictionary with the classification and model timings. It sends
the 80 cases followed by four repeated examples of each class. A distinct
publisher and gateway communicate through a real local MQTT broker. Only the
sensor values and request identifiers are sent to the gateway.

The callback receives one result dictionary per attempted request. `seq`
starts at 1 and `repeated` marks attempts 81–100. The status is "ok",
"timeout", or "error". Times are milliseconds; `inference_ms` measures the
gateway model call and `total_ms` measures from request publication to
response reception on the sender. When supplied, `model_input_at` and
`model_output_at` record the UTC timestamps at that call's boundaries.
Errors and timeouts have no timing result.
The return value is the list of recorded attempts. Connection/startup failures
raise exceptions; they are never replaced by an in-process transport.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from collections import Counter


def _schedule(cases):
    if len(cases) != 80 or len({case["udi"] for case in cases}) != 80:
        raise ValueError("서로 다른 시험 사례 80건이 필요합니다.")
    counts = Counter(case["expected"] for case in cases)
    if len(counts) != 5 or min(counts.values()) < 4:
        raise ValueError("반복 시험을 위해 5개 유형에 각각 4건 이상이 필요합니다.")
    repeats = []
    for label in counts:
        examples = [case for case in cases if case["expected"] == label]
        repeats.extend(examples[:4])
    return [(case, False) for case in cases] + [(case, True) for case in repeats]


def run_mqtt(cases, predict, on_record=None, stop_event=None, timeout=10.0):
    """Run local MQTT benchmark. This function is blocking; call from a worker."""
    schedule = _schedule(cases)
    if not callable(predict):
        raise TypeError("모델 판단 함수를 확인하세요.")
    if timeout <= 0:
        raise ValueError("시간 제한은 0보다 커야 합니다.")
    return asyncio.run(_run(schedule, predict, on_record, stop_event or threading.Event(), timeout))


async def _run(schedule, predict, on_record, stop_event, timeout):
    from amqtt.broker import Broker
    from paho.mqtt import client as mqtt

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    broker = Broker({
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
        "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}},
    })
    loop = asyncio.get_running_loop()
    gateway_requests = asyncio.Queue()
    sensor_responses = asyncio.Queue()
    gateway_subscribed = asyncio.Event()
    sensor_subscribed = asyncio.Event()
    shutting_down = threading.Event()
    clients = []
    worker = None
    started_broker = False
    run_id = uuid.uuid4().hex
    request_topic = f"laya-edge-bench/{run_id}/request"
    response_topic = f"laya-edge-bench/{run_id}/response"

    def make_client(name, topic, subscribed, inbox):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{name}-{run_id}")

        def on_connect(client, userdata, flags, reason_code, properties):
            if not reason_code.is_failure:
                client.subscribe(topic, qos=1)

        def on_subscribe(client, userdata, mid, reason_codes, properties):
            if reason_codes and all(not code.is_failure for code in reason_codes):
                loop.call_soon_threadsafe(subscribed.set)

        def on_message(client, userdata, message):
            loop.call_soon_threadsafe(inbox.put_nowait, bytes(message.payload))

        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message
        return client

    async def gateway_loop(client):
        seen = set()
        while True:
            payload = await gateway_requests.get()
            if payload is None:
                return
            if shutting_down.is_set() or stop_event.is_set():
                continue
            try:
                request = json.loads(payload)
                key = (request["run_id"], request["seq"])
                if request["run_id"] != run_id or key in seen:
                    continue
                seen.add(key)
                started = time.perf_counter()
                try:
                    prediction = await asyncio.to_thread(predict, request["sensors"])
                    if isinstance(prediction, str):
                        elapsed = (time.perf_counter() - started) * 1000
                        prediction = {"prediction": prediction, "inference_ms": round(elapsed, 3)}
                    response = {"run_id": run_id, "seq": request["seq"], "status": "ok",
                                "prediction": prediction["prediction"],
                                "model_input_at": prediction.get("model_input_at"),
                                "model_output_at": prediction.get("model_output_at"),
                                "inference_ms": prediction.get("inference_ms")}
                except Exception as exc:
                    response = {"run_id": run_id, "seq": request["seq"], "status": "error",
                                "error": f"{type(exc).__name__}: {exc}"}
                client.publish(response_topic, json.dumps(response, ensure_ascii=False), qos=1)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue

    rows = []
    try:
        await broker.start()
        started_broker = True
        gateway = make_client("gateway", request_topic, gateway_subscribed, gateway_requests)
        sensor = make_client("sensor", response_topic, sensor_subscribed, sensor_responses)
        for client in (gateway, sensor):
            clients.append(client)
            client.connect("127.0.0.1", port)
            client.loop_start()
        await asyncio.wait_for(asyncio.gather(gateway_subscribed.wait(), sensor_subscribed.wait()), 10)
        worker = asyncio.create_task(gateway_loop(gateway))

        for seq, (case, repeated) in enumerate(schedule, start=1):
            if stop_event.is_set():
                break
            request = {"run_id": run_id, "seq": seq, "sensors": case["sensors"]}
            row = {"seq": seq, "udi": case["udi"], "expected": case["expected"],
                   "prediction": None, "repeated": repeated, "status": "timeout",
                   "model_input_at": None, "model_output_at": None,
                   "inference_ms": None, "total_ms": None, "error": ""}
            started = time.perf_counter()
            published = sensor.publish(request_topic, json.dumps(request, ensure_ascii=False, allow_nan=False), qos=1)
            if published.rc != mqtt.MQTT_ERR_SUCCESS:
                row.update(status="error", error=f"MQTT 발행 오류 ({published.rc})")
            else:
                deadline = started + timeout
                while not stop_event.is_set():
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        row["error"] = "응답 시간 초과"
                        break
                    try:
                        data = await asyncio.wait_for(sensor_responses.get(), min(remaining, 0.2))
                    except asyncio.TimeoutError:
                        continue
                    try:
                        response = json.loads(data)
                        if response.get("run_id") != run_id or response.get("seq") != seq:
                            continue
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    total = (time.perf_counter() - started) * 1000
                    if response.get("status") == "ok" and isinstance(response.get("prediction"), str):
                        row.update(status="ok", prediction=response["prediction"],
                                   model_input_at=response.get("model_input_at"),
                                   model_output_at=response.get("model_output_at"),
                                   inference_ms=response.get("inference_ms"), total_ms=round(total, 3))
                    else:
                        row.update(status="error", error=str(response.get("error", "유효하지 않은 모델 응답")))
                    break
            if stop_event.is_set():
                break
            rows.append(row)
            if on_record:
                on_record(dict(row))
        return rows
    finally:
        if worker is not None:
            shutting_down.set()
            await gateway_requests.put(None)
            await worker
        for client in clients:
            client.disconnect()
            await asyncio.to_thread(client.loop_stop)
        if started_broker:
            await broker.shutdown()
