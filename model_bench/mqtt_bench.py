"""Run a request/response MQTT benchmark with local or Docker inference.

`run_mqtt(cases, predict, on_record=None, stop_event=None, timeout=10.0)`
expects 80 dictionaries with udi, expected and sensors, and a callable that
accepts a sensors dictionary and returns a classification string or a
dictionary with the classification and model timings. It sends
the 80 cases followed by four repeated examples of each class. A distinct
publisher and gateway communicate through a real local MQTT broker. Only the
sensor values and request identifiers are sent to the gateway.
For Docker inference, pass broker_address and omit the local predict callable.

The default is 100 requests. Larger runs replay the same sequence, and
`repeated` marks every attempt after the first 80. The callback receives one
dictionary per attempted request and `seq` starts at 1. The status is "ok",
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
from itertools import cycle, islice
import json
import math
import socket
from statistics import median
import threading
import time
import uuid
from collections import Counter


DEFAULT_DOCKER_PORT = 18883


def prediction_response(predict, request):
    response = {"run_id": request["run_id"], "seq": request["seq"]}
    try:
        started = time.perf_counter()
        prediction = predict(request["sensors"])
        if isinstance(prediction, str):
            elapsed = (time.perf_counter() - started) * 1000
            prediction = {"prediction": prediction, "inference_ms": round(elapsed, 3)}
        response.update(status="ok", prediction=prediction["prediction"],
                        model_input_at=prediction.get("model_input_at"),
                        model_output_at=prediction.get("model_output_at"),
                        inference_ms=prediction.get("inference_ms"))
    except Exception as exc:
        response.update(status="error", error=f"{type(exc).__name__}: {exc}")
    return response


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


def run_mqtt(cases, predict=None, on_record=None, stop_event=None, timeout=10.0,
             *, broker_address=None, on_gateway=None, request_count=100):
    """Run the MQTT benchmark. This function is blocking; call from a worker."""
    if type(request_count) is not int or request_count < 1:
        raise ValueError("요청 횟수는 양의 정수여야 합니다.")
    base_schedule = _schedule(cases)
    if broker_address is None and not callable(predict):
        raise TypeError("모델 판단 함수를 확인하세요.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("시간 제한은 유한한 양수여야 합니다.")
    schedule = (
        (case, repeated or index >= len(base_schedule))
        for index, (case, repeated) in enumerate(islice(cycle(base_schedule), request_count))
    )
    return asyncio.run(_run(schedule, predict, on_record, stop_event or threading.Event(),
                            timeout, broker_address, on_gateway))


def latency_summary(rows, key):
    """Return the median and nearest-rank p95 of successful, timed replies."""
    times = sorted(
        row[key] for row in rows
        if row["status"] == "ok" and type(row[key]) in (int, float)
        and math.isfinite(row[key]) and row[key] >= 0
    )
    if not times:
        return None, None
    return median(times), times[math.ceil(.95 * len(times)) - 1]


async def _run(schedule, predict, on_record, stop_event, timeout, broker_address, on_gateway):
    from paho.mqtt import client as mqtt

    broker = None
    if broker_address is None:
        from amqtt.broker import Broker

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            host, port = probe.getsockname()
        broker = Broker({
            "listeners": {"default": {"type": "tcp", "bind": f"{host}:{port}"}},
            "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}},
        })
    else:
        host, port = broker_address
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
                response = await asyncio.to_thread(prediction_response, predict, request)
                client.publish(response_topic, json.dumps(response, ensure_ascii=False), qos=1)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue

    async def exchange(sensor, request, wait_seconds):
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False)
        started = time.perf_counter()
        published = sensor.publish(request_topic, payload, qos=1)
        if published.rc != mqtt.MQTT_ERR_SUCCESS:
            return {"status": "error", "error": f"MQTT 발행 오류 ({published.rc})"}, None

        deadline = started + wait_seconds
        while not stop_event.is_set():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(sensor_responses.get(), min(remaining, 0.2))
            except asyncio.TimeoutError:
                continue
            elapsed = (time.perf_counter() - started) * 1000
            try:
                response = json.loads(data)
            except (ValueError, UnicodeError):
                continue
            if (isinstance(response, dict) and response.get("run_id") == run_id
                    and response.get("seq") == request["seq"]):
                return response, round(elapsed, 3)
        return None, None

    rows = []
    try:
        sensor = make_client("sensor", response_topic, sensor_subscribed, sensor_responses)
        subscribers = [(sensor, sensor_subscribed)]
        if broker is not None:
            await broker.start()
            started_broker = True
            gateway = make_client("gateway", request_topic, gateway_subscribed, gateway_requests)
            subscribers.append((gateway, gateway_subscribed))
        for client, _ in subscribers:
            clients.append(client)
            client.connect(host, port)
            client.loop_start()
        await asyncio.wait_for(asyncio.gather(*(ready.wait() for _, ready in subscribers)), 10)
        if broker is not None:
            worker = asyncio.create_task(gateway_loop(gateway))
        else:
            ping = {"run_id": run_id, "seq": 0, "kind": "ready"}
            reply, _ = await exchange(sensor, ping, 5)
            if stop_event.is_set():
                return rows
            if not reply or reply.get("status") != "ready" or not isinstance(reply.get("gateway"), dict):
                raise RuntimeError("Docker 게이트웨이 응답이 없습니다. 모델 로딩과 컨테이너 로그를 확인하세요.")
            if on_gateway:
                startup_metrics = dict(reply["gateway"])
                startup_metrics.pop("memory_peak_mib", None)
                on_gateway(startup_metrics)

        for seq, (case, repeated) in enumerate(schedule, start=1):
            if stop_event.is_set():
                break
            request = {"run_id": run_id, "seq": seq, "sensors": case["sensors"]}
            row = {"seq": seq, "udi": case["udi"], "expected": case["expected"],
                   "prediction": None, "repeated": repeated, "status": "timeout",
                   "model_input_at": None, "model_output_at": None,
                   "inference_ms": None, "total_ms": None, "error": ""}
            response, total = await exchange(sensor, request, timeout)
            if response is None:
                row["error"] = "응답 시간 초과"
            elif response.get("status") == "ok" and isinstance(response.get("prediction"), str):
                row.update(status="ok", prediction=response["prediction"],
                           model_input_at=response.get("model_input_at"),
                           model_output_at=response.get("model_output_at"),
                           inference_ms=response.get("inference_ms"), total_ms=total)
            else:
                row.update(status="error", error=str(response.get("error", "유효하지 않은 모델 응답")))
            if stop_event.is_set():
                break
            rows.append(row)
            if on_record:
                on_record(dict(row))
        if broker is None and on_gateway and not stop_event.is_set():
            final, _ = await exchange(sensor, ping, 2)
            if final and final.get("status") == "ready" and isinstance(final.get("gateway"), dict):
                on_gateway(final["gateway"])
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
