# Digging Market — 셀러/소비자용 웹앱

플리마켓 현장에서 QR로 접속해 쓰는 결제 없는 등록/발견 도구입니다.
셀러는 사진을 올리면 AI가 카테고리·설명 초안을 잡아주고, 소비자는 피드를 구경하다 찜하고
셀러에게 연락(외부 채널)해서 거래를 마무리합니다.

## 화면 구성

- `/` — 랜딩 (행사 정보 + 구경하기/셀러등록 진입)
- `/sell` — 셀러 상품 등록 (사진 → AI 자동분석 → 확인/수정 → 등록)
- `/feed` — 소비자 피드 (카테고리 필터, 찜, 상세보기, 셀러 연락)
- `/dashboard` — 운영자용 검증항목 대시보드 (재고소진율, 조회/찜, 설문 응답)

## 로컬 실행

```bash
pip install -r requirements.txt --break-system-packages
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

브라우저에서 `http://localhost:8000` 접속.

## AI 이미지 분석 활성화 (선택)

환경변수 `ANTHROPIC_API_KEY`를 설정하면 Claude Vision으로 실제 이미지 분석을 합니다.
설정하지 않으면 파일명 기반의 단순 규칙으로 폴백해 개발/데모는 계속 가능합니다.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Render 배포

1. GitHub에 이 폴더를 push
2. Render 대시보드 → New Web Service → 리포지토리 연결
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. (선택) AI 분석 쓰실 거면 Environment → `ANTHROPIC_API_KEY` 추가
6. 행사 당일에는 Starter 플랜(월 $7)으로 잠깐 올려서 슬립모드(15분 비활성 시 대기) 방지 권장

### 중요: 데이터 유실 방지 (Persistent Disk 설정)

Render는 기본적으로 파일시스템이 임시(ephemeral)라서, **디스크를 안 붙이면 재배포/재시작마다 DB와 업로드된 사진이 전부 사라집니다.**
Starter 이상 플랜에서만 디스크를 붙일 수 있으니, 행사 기간에는 반드시 아래를 설정하세요.

1. 서비스가 Starter 플랜인지 확인 (Free 플랜은 디스크 연결 불가)
2. 서비스 페이지 → **Disks** 탭 → **Add Disk**
3. Mount Path에 `/var/data` 입력, 용량은 1GB로 충분
4. 서비스 → **Environment** 탭 → 환경변수 추가:
   - Key: `DATA_DIR`
   - Value: `/var/data`
5. 저장하면 자동으로 재배포되고, 이후부터 DB(`digging_market.db`)와 업로드된 사진이 `/var/data`에 저장되어 재배포/재시작에도 유지됩니다

`DATA_DIR`을 설정하지 않으면 (로컬 개발 시처럼) 프로젝트 폴더에 그대로 저장됩니다 — 로컬 테스트에는 영향 없습니다.

행사 끝나면 Starter 플랜과 디스크를 다시 내리셔도 됩니다 (그 전에 대시보드에서 데이터 백업 권장 — DB 파일은 Shell에서 다운받거나, `/api/stats`, `/api/items` 응답을 미리 저장해두는 것도 방법입니다).

## 데이터

SQLite 파일(`digging_market.db`)에 저장됩니다. 이미지는 `uploads/` 폴더에 저장됩니다.
행사 하루~일주일짜리 용도로는 이 구성으로 충분하며, 별도 DB 서버가 필요하지 않습니다.

## 스코프 밖 (의도적으로 뺀 것)

- 결제/정산 기능 — 거래는 셀러 연락처(인스타/오픈채팅)로 넘어가서 앱 밖에서 진행
- 회원가입/로그인 — 소비자는 로그인 없이 바로 이용, 셀러도 이름만 입력
- 부스 배치 편집기 — 부스 번호는 텍스트로만 입력, 배치도는 현장에 별도 게시
