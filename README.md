# Cloud Control

실제 AWS 리소스를 웹에서 조회·제어·점검하는 학습용 앱입니다. (WSL Ubuntu · FastAPI · HTML/CSS/JS · Boto3 · MariaDB)

| 화면 | 내용 |
|---|---|
| 대시보드 | 계정·호출자 ARN·리전, 리소스 수 요약 (조회 실패는 "확인 불가") |
| EC2 | 인스턴스 목록, 상태 필터(이미 조회한 목록에서 거름), 허용 대상 시작·중지 |
| S3 | 계정의 버킷 목록 |
| 보안 그룹 | 서울 리전 보안 그룹 인바운드 규칙 |
| 실행 이력 | 시작·중지 이력(최근 50건)과 허용 대상 EC2 현재 상태 |
| 보안 점검 | 실제 EC2 ENI에 연결된 보안 그룹의 공개 TCP 22·3389 규칙 탐지 |

범위 밖: 일반 할 일, 자동 수정, 리소스 생성·삭제, 예약, 사용자 관리, 외부 배포.

## 구조

```
backend/
  config.py          .env 읽기 (서버 실행 시에만), 필수값·리전 검증
  auth.py            모든 /api 공통 Bearer 토큰 인증
  aws_service.py     Boto3 조회·제어·보안 그룹 수집, AWS 오류 분류
  security_check.py  inspect_group: 공개 22·3389 규칙 판정
  actions.py         EC2 시작·중지 (중복 키·lock·이력 순서)
  db.py / models.py  SQLAlchemy+PyMySQL, action_logs
  init_db.py         없는 테이블만 생성
  main.py            FastAPI 경로, frontend/ 정적 파일
frontend/            index.html, app.js, style.css
tests/               unittest (AWS·DB 대체, 실제 호출 없음)
check_api.py         토큰을 getpass로 입력받아 API 확인
```

## API

모든 `/api` 경로는 `Authorization: Bearer <ADMIN_API_TOKEN>`이 필요합니다. 오류 본문은 `{"detail":{"code":"...","message":"..."}}`입니다.

| 메서드·경로 | 응답 |
|---|---|
| `GET /api/context` | `account_id`, `caller_arn`, `region` |
| `GET /api/ec2` | `items[{instance_id,name,state,instance_type,private_ip,public_ip,controllable}]`, `count` |
| `GET /api/s3` | `items[{name}]`, `count` |
| `GET /api/security-groups` | `items[{GroupId,GroupName,IpPermissions}]`, `count` |
| `POST /api/ec2/{instance_id}/actions` | 본문 `{action:"start"\|"stop", request_id:UUID}` → 202 접수 / 200 같은 키 재전송 |
| `GET /api/actions` | 최근 50건 `items`, `count` |
| `GET /api/security-checks` | `findings`, `checked_groups`, `checked_instance_count`, `checked_group_count`, `finding_count`, `checked_at` |

주요 오류 코드:

| 코드 | 의미 | 조치 |
|---|---|---|
| `APP_AUTH_REQUIRED` (401) | 앱 토큰 없음·불일치 | 화면에서 토큰 다시 입력 |
| `AWS_LOGIN_REQUIRED` (503) | WSL의 AWS 로그인 만료·갱신 불가 | `aws login --profile lab` 후 다시 조회 |
| `AWS_ACCESS_DENIED` (403) | AWS 권한 부족 | IAM 권한 확인 |
| `AWS_ACCOUNT_MISMATCH` (403) | 로그인 계정 ≠ `EXPECTED_ACCOUNT_ID` | 프로필·설정 확인 |
| `AWS_CONNECTION_ERROR` (503) | AWS 통신 실패 | 네트워크 확인 후 재시도 |
| `AWS_SCAN_INCOMPLETE` (503) | 점검 중 대상 불일치·누락 | 다시 점검 |
| `AWS_RESULT_UNKNOWN` (503) | 제어 결과 불확실 (UNKNOWN 기록) | 콘솔에서 상태 확인, 자동 재요청 없음 |
| `INSTANCE_NOT_ALLOWED` / `CONTROL_DISABLED` (403) | 허용 목록 밖 / 허용 목록 비어 있음 | 서버 `.env` 확인 |
| `INVALID_STATE` / `ACTION_IN_PROGRESS` / `IDEMPOTENCY_CONFLICT` (409) | 상태 불일치 / 미확정 이력 있음 / 같은 키 다른 내용 | 새로고침 후 확인 |
| `DB_UNAVAILABLE` / `DB_NOT_INITIALIZED` (503) | DB 연결 실패 / 테이블 없음 | MariaDB 상태, `init_db` |

## 설정

`.env.example`을 복사해 `.env`를 만들고 값을 채웁니다. `.env`는 Git에 올리지 않습니다.

| 변수 | 설명 |
|---|---|
| `AWS_PROFILE` | `aws login`에 쓴 프로필 (예: `lab`) |
| `AWS_REGION` | `ap-northeast-2`만 허용 |
| `EXPECTED_ACCOUNT_ID` | 12자리 계정 ID. 다르면 403 |
| `ADMIN_API_TOKEN` | 앱 토큰. 없거나 비어 있으면 서버 시작 거절 |
| `ALLOWED_INSTANCE_IDS` | 시작·중지를 허용할 인스턴스 ID (쉼표 구분). 비어 있으면 제어 전부 거절, 조회는 허용. 보안 점검 대상 필터로는 쓰지 않음 |
| `DB_HOST` `DB_PORT` `DB_NAME` `DB_USER` `DB_PASSWORD` | MariaDB 접속 정보 |

토큰은 출력하지 않고 만듭니다:

```bash
echo "ADMIN_API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
```

## 처음 설치

```bash
cd ~/vibe-lab/cloud-control
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# MariaDB: DB와 앱 사용자 (비밀번호는 .env의 DB_PASSWORD)
sudo mariadb -e "CREATE DATABASE IF NOT EXISTS clouddb CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
# 사용자 생성 후 권한: 평소 SELECT, INSERT, UPDATE / 테이블 생성 시에만 CREATE, INDEX
sudo mariadb -e "GRANT CREATE, INDEX ON clouddb.* TO 'cloudapp'@'127.0.0.1';"
python -m backend.init_db          # 없는 테이블만 생성, 기존 데이터 유지
sudo mariadb -e "REVOKE CREATE, INDEX ON clouddb.* FROM 'cloudapp'@'127.0.0.1';"
```

## 실행

```bash
# 터미널 1: 서버 (단일 프로세스, 127.0.0.1:8001)
cd ~/vibe-lab/cloud-control && source .venv/bin/activate
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8001
```

Windows 브라우저에서 http://127.0.0.1:8001 을 열고 앱 토큰을 입력합니다.
토큰은 브라우저 메모리에만 있고 URL·localStorage에 저장하지 않으므로 새로고침하면 다시 입력합니다.

`address already in use`가 나오면 이전 서버가 떠 있는 것입니다: `pkill -f "uvicorn backend.main:app"` 후 다시 실행.

테스트 (실제 AWS·DB를 호출하지 않음):

```bash
python -m unittest discover -s tests -v
```

## 최종 확인 절차

터미널 2에서 진행합니다. 실제 제어는 필요할 때만 화면에서 직접 수행하고 반복하지 않습니다.

```bash
cd ~/vibe-lab/cloud-control && source .venv/bin/activate
export AWS_PAGER=""
```

**1. AWS 로그인과 STS 계정**

```bash
aws sts get-caller-identity --profile lab
```

`Account`가 `.env`의 `EXPECTED_ACCOUNT_ID`와 같아야 합니다.

**2. MariaDB와 앱 실행**

```bash
systemctl is-active mariadb        # active
python check_api.py /api/context   # HTTP 200, 1번과 같은 계정·ARN
```

**3. 브라우저에서 앱 토큰 입력** → 대시보드에 계정·리전·숫자가 표시되는지 확인.

**4. EC2·S3·보안 그룹 목록을 CLI와 대조**

```bash
aws ec2 describe-instances --profile lab --region ap-northeast-2 \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,InstanceType,PrivateIpAddress,PublicIpAddress]' --output table
aws s3api list-buckets --profile lab --query 'Buckets[].Name' --output table
aws ec2 describe-security-groups --profile lab --region ap-northeast-2 --query 'length(SecurityGroups)'
```

화면의 EC2 개수·상태·IP, S3 버킷 이름, 보안 그룹 개수(대시보드)와 같아야 합니다.

**5. 실행 이력과 EC2 현재 상태** → "실행 이력" 탭에서 7장에서 검증한 시작·중지 이력(ACCEPTED)과 대상 인스턴스의 현재 상태를 확인합니다.

**6. 보안 점검을 CLI와 대조** → "보안 점검" 탭에서 "점검 실행" 후 아래 결과와 비교합니다.

```bash
# 실제 EC2(종료 제외) ENI에 연결된 보안 그룹 (인스턴스·ENI·그룹)
aws ec2 describe-instances --profile lab --region ap-northeast-2 \
  --filters "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].NetworkInterfaces[].[Attachment.InstanceId,NetworkInterfaceId,join(`,`,Groups[].GroupId)]' \
  --output table

# 그중 0.0.0.0/0 또는 ::/0 에 열린 인바운드 규칙
SG_IDS=$(aws ec2 describe-instances --profile lab --region ap-northeast-2 \
  --filters "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].NetworkInterfaces[].Groups[].GroupId' --output text | tr '\t' '\n' | sort -u)
aws ec2 describe-security-groups --profile lab --region ap-northeast-2 --group-ids $SG_IDS \
  --query 'SecurityGroups[].{ID:GroupId,Name:GroupName,Public:IpPermissions[?IpRanges[?CidrIp==`0.0.0.0/0`] || Ipv6Ranges[?CidrIpv6==`::/0`]].[IpProtocol,FromPort,ToPort]}' \
  --output json
```

점검 대상 그룹 수 = 첫 명령의 고유 그룹 수, 발견 = 두 번째 결과 중 TCP 22·3389를 포함하거나 프로토콜 `-1`인 규칙입니다.
콘솔(EC2 → 보안 그룹 → 인바운드 규칙)에서도 같은 규칙을 확인합니다.

**7. 토큰 없이 호출하면 401**

```bash
curl -s -w '\n%{http_code}\n' http://127.0.0.1:8001/api/ec2
curl -s -w '\n%{http_code}\n' http://127.0.0.1:8001/api/security-checks
```

둘 다 `APP_AUTH_REQUIRED`, `401`이어야 합니다.

**8. 앱을 재시작해도 실행 이력 유지**

```bash
pkill -f "uvicorn backend.main:app"     # 터미널 1에서 다시 실행
python check_api.py /api/actions        # 재시작 전과 같은 이력
```

**9. AWS 로그인 만료 시 복구 절차**

1. 화면에 빨간 배너 `[AWS_LOGIN_REQUIRED]`가 나타납니다. 앱 토큰 문제가 아니므로 다시 입력하지 않습니다.
2. WSL에서 `aws login --profile lab`을 실행해 다시 로그인합니다.
3. `aws sts get-caller-identity --profile lab`으로 계정을 확인합니다.
4. 앱 서버는 재시작하지 않아도 됩니다. 화면에서 "조회"를 누르면 복구됩니다.

## 운영 메모

- 시작·중지는 `REQUESTED` 기록 → AWS 1회 호출 → `ACCEPTED`/`FAILED`/`UNKNOWN` 순서입니다. 자동 재시도·waiter·폴링은 없습니다.
- `UNKNOWN`이나 남아 있는 `REQUESTED`는 같은 대상의 새 제어를 막습니다. 콘솔에서 실제 결과를 확인한 뒤 관리자가 정리합니다:
  ```bash
  sudo mariadb clouddb -e "UPDATE action_logs SET status='FAILED', message='콘솔 확인 후 수동 정리' WHERE id=<번호> AND status IN ('UNKNOWN','REQUESTED');"
  ```
- 보안 점검은 규칙 기반이며 실제 침해 여부 판정이 아닙니다. 결과는 저장하지 않고 요청마다 새로 조회합니다.
- 비밀값(`ADMIN_API_TOKEN`, `DB_PASSWORD`, AWS 자격증명)은 브라우저 코드·Git·로그·AI 대화에 넣지 않습니다.
