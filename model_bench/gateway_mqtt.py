"""Run the AI4I model on the gateway side of the MQTT benchmark."""
from collections import OrderedDict
from functools import partial
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import signal
from threading import Event

from paho.mqtt import client as mqtt

from ai4i_evaluation import load_trial, predict_sensors
from ai4i_simulation import CASES, SENSOR_KEYS
from mqtt_bench import prediction_response


LOGGER = logging.getLogger(__name__)
TOPIC = re.compile(r"laya-edge-bench/([0-9a-f]{32})/request")
CGROUP = Path("/sys/fs/cgroup")


def resource_metrics():
    def read_metric(name):
        try:
            return (CGROUP / name).read_text(encoding="ascii").strip()
        except OSError:
            return ""

    cpu = read_metric("cpu.max").split()
    limit = read_metric("memory.max")
    peak = read_metric("memory.peak")
    return {
        "cpu_limit": int(cpu[0]) / int(cpu[1]) if len(cpu) == 2 and cpu[0].isdigit() else None,
        "memory_limit_mib": round(int(limit) / (1024 * 1024), 2) if limit.isdigit() else None,
        "memory_peak_mib": round(int(peak) / (1024 * 1024), 2) if peak.isdigit() else None,
    }


def serve(host, port, predict, metadata, stop):
    requests = Queue(maxsize=128)
    replies = OrderedDict()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOGGER.error("MQTT connection failed: %s", reason_code)
        else:
            client.subscribe("laya-edge-bench/+/request", qos=1)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        if reason_codes and all(not code.is_failure for code in reason_codes):
            LOGGER.info("Gateway ready. Model: %s", metadata["model"])
        else:
            LOGGER.error("MQTT subscription failed: %s", reason_codes)

    def on_message(client, userdata, message):
        match = TOPIC.fullmatch(message.topic)
        if not match or message.retain or len(message.payload) > 65536:
            return
        try:
            requests.put_nowait((match[1], bytes(message.payload)))
        except Full:
            LOGGER.warning("Gateway request queue is full")

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.connect_async(host, port)
    client.loop_start()
    try:
        while not stop.is_set():
            try:
                run_id, payload = requests.get(timeout=0.2)
            except Empty:
                continue
            try:
                request = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(request, dict) or request.get("run_id") != run_id:
                continue
            seq = request.get("seq")
            if type(seq) is not int or seq < 0:
                continue

            if seq == 0 and request.get("kind") == "ready":
                response = {"run_id": run_id, "seq": seq, "status": "ready",
                            "gateway": {**metadata, **resource_metrics()}}
            elif seq > 0:
                key = (run_id, seq)
                if key not in replies:
                    replies[key] = prediction_response(predict, request)
                    if len(replies) > 256:
                        replies.popitem(last=False)
                response = replies[key]
            else:
                continue
            client.publish(f"laya-edge-bench/{run_id}/response",
                           json.dumps(response, ensure_ascii=False), qos=1)
    finally:
        client.disconnect()
        client.loop_stop()


def main():
    import torch

    threads = int(os.environ.get("LAYA_TORCH_THREADS", "2"))
    torch.set_num_threads(threads)
    torch.set_num_interop_threads(1)
    model_path = Path(os.environ["LAYA_AI4I_MODEL"])
    stop = Event()

    def request_stop(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    LOGGER.info("Loading model from %s", model_path)
    trial = load_trial(model_path)
    predict = partial(predict_sensors, trial)
    warmup = CASES[:3]
    for _, _, values in warmup:
        if stop.is_set():
            return
        predict(dict(zip(SENSOR_KEYS, values)))
    metadata = {
        "model": os.environ.get("LAYA_MODEL_LABEL") or model_path.name,
        "features": trial.features,
        "torch_threads": torch.get_num_threads(),
        "warmup_count": len(warmup),
        "laya_version": version("laya"),
        "torch_version": str(torch.__version__),
    }
    serve(os.environ.get("LAYA_MQTT_HOST", "broker"),
          int(os.environ.get("LAYA_MQTT_BROKER_PORT", "1883")), predict, metadata, stop)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
