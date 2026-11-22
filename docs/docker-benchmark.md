# Docker 게이트웨이 MQTT 벤치마크

Windows PC에서 AI4I 시험 데이터를 MQTT로 재생하고, Docker 게이트웨이에서 실행하는 Laya 기준 모델의 분류 성능과 지연시간을 측정합니다. 측정 앱은 100회 실행과 Excel 내보내기를 지원합니다. 명령줄에서는 같은 입력 순서로 1,000회 실행하고 요약과 요청별 결과를 JSON으로 저장합니다.

```text
Windows 측정 도구 ↔ Mosquitto 브로커 ↔ Laya 추론 게이트웨이
                      Docker Desktop 컨테이너 2개
                                      /model ← Docker 모델 볼륨
```

Windows의 측정 도구가 센서값을 보내면, 브로커가 Docker 내부의 추론 게이트웨이에 전달합니다. 게이트웨이는 모델을 실행해 예측값과 추론 시간을 반환합니다. 측정 도구는 예측값을 정답과 대조하고 MQTT 왕복 시간을 기록합니다.

## 1. 준비

Git, Python 3.12, Docker Desktop이 설치된 Windows에서 PowerShell을 엽니다. Docker Desktop을 실행하고 아래 명령에서 `docker info`의 출력이 `linux`인지 확인합니다. 최초 이미지 빌드와 패키지 설치에는 인터넷 연결이 필요합니다. 이후 MQTT 통신과 모델 추론은 PC 안에서 이루어집니다. [Docker Desktop 설치 안내](https://docs.docker.com/desktop/setup/install/windows-install/)

```powershell
git clone https://github.com/parang12/laya-edge-bench.git
Set-Location .\laya-edge-bench

docker info --format '{{.OSType}}'
docker compose version
py -3.12 -m venv .\model_bench\.venv
.\model_bench\.venv\Scripts\python.exe -m pip install -r .\model_bench\requirements.txt
```

FP32 기준 모델 릴리스에서 `laya-ai4i-fp32-B_margin_500.zip`을 내려받아 압축을 풉니다. 다음 단계에서 지정할 폴더의 바로 아래에 `model.safetensors`, `rl_agent_config.json`, `questions.json`, `run_config.json`, `encoder/`, `tokenizer/`가 있어야 합니다. [FP32 기준 모델 릴리스](https://github.com/parang12/laya-edge-bench/releases/tag/ai4i-b-margin-fp32)

릴리스 ZIP의 SHA-256은 `fa023eb9ae135083ef1a2bdd8bc0b114259bd1a70ba54f522cfb728b414ae403`입니다. PowerShell의 `Get-FileHash -Algorithm SHA256`에 내려받은 ZIP 경로를 지정해 확인할 수 있습니다.

센서값과 정답은 저장소의 [고정 시험 사례 80건](../model_bench/ai4i_test_cases.json)에 포함되어 있습니다. 이 파일은 설비 예지보전용 합성 데이터인 AI4I 2020에서 선택한 사례를 기록합니다. [UCI AI4I 2020 Predictive Maintenance Dataset](https://doi.org/10.24432/C5HS5C)

## 2. 모델을 Docker 볼륨에 복사

아래 경로의 예시를 압축을 푼 위치로 바꿉니다. 최초 복사는 빈 볼륨에서 실행합니다. 같은 모델을 다시 측정할 때는 볼륨을 재사용하고 `docker run` 복사를 건너뜁니다. 게이트웨이는 볼륨을 `/model`에 읽기 전용으로 연결합니다.

```powershell
$modelDir = (Resolve-Path 'C:\models\laya-ai4i-fp32-B_margin_500').Path
$env:LAYA_MODEL_VOLUME = 'laya-edge-bench-model-b-margin-500'
$env:LAYA_MODEL_LABEL = 'laya-ai4i-fp32-B_margin_500'

docker volume create $env:LAYA_MODEL_VOLUME
docker run --rm --network none `
  --mount "type=bind,source=$modelDir,target=/source,readonly" `
  --mount "type=volume,source=$env:LAYA_MODEL_VOLUME,target=/target,volume-nocopy" `
  python:3.12-slim sh -ceu 'test -f /source/model.safetensors; if find /target -mindepth 1 -print -quit | grep -q .; then echo target_volume_must_be_empty >&2; exit 1; fi; cp -a /source/. /target/; diff -qr /source /target'
```

## 3. 게이트웨이 실행

같은 PowerShell에서 CPU 2코어 분량, 컨테이너 메모리 2 GiB, PyTorch 연산 스레드 2개를 지정합니다. Compose는 메모리 한도와 메모리·스왑 합계 한도를 같은 값으로 설정하므로 게이트웨이는 스왑을 사용하지 않습니다. [Docker 메모리·스왑 설정](https://docs.docker.com/engine/containers/resource_constraints/#--memory-swap-details)

```powershell
$env:LAYA_GATEWAY_CPUS = '2'
$env:LAYA_GATEWAY_MEMORY = '2g'
$env:LAYA_TORCH_THREADS = '2'
$env:LAYA_MQTT_PORT = '18883'

docker compose -f .\model_bench\compose.yaml up -d --build
docker compose -f .\model_bench\compose.yaml ps
docker compose -f .\model_bench\compose.yaml logs --tail 30 gateway
```

게이트웨이는 모델을 읽고 예열 추론 3회를 실행합니다. 로그에 `Gateway ready. Model: laya-ai4i-fp32-B_margin_500`이 보이면 측정할 수 있습니다. Docker Desktop의 **Containers → laya-edge-bench**에서도 브로커와 게이트웨이의 상태 및 로그를 확인할 수 있습니다. [Docker Desktop 컨테이너 화면](https://docs.docker.com/desktop/use-desktop/container/)

## 4. 연결 확인과 1,000회 측정

### 연결 확인

아래 명령으로 MQTT 요청 5건을 보냅니다. 측정 앱을 쓸 경우 저장소 최상위 폴더에서 `.\모델_측정_실행.cmd`를 실행하고 **Docker MQTT · 100회**를 누릅니다. 이 버튼은 Docker 볼륨의 모델을 사용합니다. 측정 후 **엑셀로 내보내기**를 누르면 요약과 요청별 결과를 저장할 수 있습니다.

```powershell
.\model_bench\.venv\Scripts\python.exe .\model_bench\benchmark_mqtt.py --requests 5 --output results/docker-mqtt-smoke.json
```

### 1,000회 측정

연결 확인 후 게이트웨이를 새로 만들어 모델 로딩과 예열부터 최대 메모리를 측정할 수 있게 합니다. `Gateway ready` 로그를 다시 확인하고 1,000회 명령을 실행합니다.

```powershell
docker compose -f .\model_bench\compose.yaml up -d --no-build --force-recreate --no-deps gateway
docker compose -f .\model_bench\compose.yaml logs --tail 30 gateway
```

```powershell
.\model_bench\.venv\Scripts\python.exe .\model_bench\benchmark_mqtt.py --requests 1000 --output results/docker-mqtt-fp32.json
```

고유 사례 80건을 먼저 보내고, 5개 유형에서 4건씩 고른 20건을 이어서 보냅니다. 이 100건 순서를 10회 재생합니다. 요청은 한 번에 하나씩 보내며 응답 제한은 기본 10초입니다. 요청이 연속 5회 시간 초과되면 측정을 중단합니다. 브로커 포트를 바꿀 때는 Compose의 `LAYA_MQTT_PORT`와 명령줄의 `--port`에 같은 값을 지정합니다. `--timeout`은 요청당 제한 시간(초), `--requests`는 전송할 요청 수입니다.

반복 측정에는 실행마다 다른 `--output` 경로를 사용합니다. 결과의 `gateway.features`가 `margin`인지 확인하면 B_margin_500의 16개 특징을 사용했는지 알 수 있습니다. `configuration.test_cases_sha256`은 고정된 사례의 순서와 내용을 확인할 때 사용합니다.

## 5. 결과 읽기

| 항목 | 계산 방법 |
| --- | --- |
| 응답 성공·시간 초과·오류 | 전체 전송 요청을 기준으로 집계합니다. |
| 정확도·Macro-F1·유형별 재현율 | 고유 80건의 첫 응답을 사용합니다. 반복은 제외하고 시간 초과와 오류는 정답으로 세지 않습니다. Macro-F1은 5개 유형의 F1을 같은 비중으로 평균합니다. |
| 추론 지연 `inference_ms` | 게이트웨이의 모델 호출 직전부터 결과 반환까지입니다. |
| MQTT 왕복 지연 `total_ms` | Windows에서 요청을 발행하기 직전부터 해당 응답을 받을 때까지입니다. 브로커 전달, 게이트웨이 처리와 추론이 포함됩니다. |
| 게이트웨이 최대 메모리 | 게이트웨이 컨테이너의 메모리 최대값입니다. 정상 종료 시 모델 로딩과 예열부터 측정 종료까지 포함합니다. |

지연은 응답에 성공한 요청만 대상으로 중앙값과 p95를 계산합니다. p95는 전체 성공 요청 중 약 95%가 그 시간 이내에 처리됐음을 뜻합니다. 같은 PC에서 비교할 때는 게이트웨이 자원 한도, 입력 순서, 요청 수, 시간 제한을 맞춥니다.

명령이 오류로 끝나도 기록한 요청과 실패 원인을 JSON에서 확인할 수 있습니다. 고유 80건을 모두 시도한 경우에 분류 지표를 계산하며, 게이트웨이에서 마지막 메모리 값을 받지 못한 경우에는 `memory_peak_mib`가 `null`입니다. 기준 FP32는 Windows에서 실행을 확인했고 macOS의 실행과 지연 특성은 별도 검증이 필요합니다. INT8과 학생 모델은 각 모델의 추론 경로가 게이트웨이에 연결된 뒤 같은 요청으로 측정할 수 있습니다.

## 6. 문제 확인과 종료

| 증상 | 확인 방법 |
| --- | --- |
| Docker에 연결되지 않음 | Docker Desktop을 시작하고 `docker info --format '{{.OSType}}'`의 출력이 `linux`인지 확인합니다. |
| `Gateway ready`가 나오지 않음 | `docker compose -f .\model_bench\compose.yaml ps -a`와 게이트웨이 로그를 확인합니다. 외부 볼륨 이름과 필수 모델 파일을 확인합니다. |
| 게이트웨이가 모델 로딩 중 종료됨 | `docker inspect laya-edge-bench-gateway-1 --format '{{.State.OOMKilled}}'`가 `true`이면 메모리 한도에 도달해 종료된 것입니다. |
| MQTT 연결 실패 또는 포트 충돌 | 브로커가 `127.0.0.1:18883`에서 실행 중인지 확인합니다. 포트를 변경했으면 Compose를 다시 실행하고 측정 도구에 같은 포트를 지정합니다. |

작업이 끝나면 아래 명령으로 컨테이너를 종료합니다. 외부 모델 볼륨은 다음 측정을 위해 보관됩니다.

```powershell
docker compose -f .\model_bench\compose.yaml down
```
