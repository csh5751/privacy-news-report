# 뉴스 피드백 API 설정

GitHub Pages의 👍/👎 평가를 Cloudflare D1에 저장하는 무료 구간용 Worker입니다.

## 1. 최초 배포

PowerShell에서 다음을 실행합니다.

```powershell
cd C:\Git\pytest\feedback-worker
npm install
npx wrangler login
npx wrangler d1 create privacy-news-feedback --location apac
```

마지막 명령이 출력한 `database_id`를 `wrangler.toml`의
`REPLACE_WITH_D1_DATABASE_ID`와 바꿉니다. 그다음 실행합니다.

```powershell
npx wrangler d1 execute privacy-news-feedback --remote --file schema.sql
npx wrangler deploy
```

출력된 `https://privacy-news-feedback....workers.dev` 주소에서 `/health`를 열어
`{"ok":true}`가 보이는지 확인합니다.

## 2. 프로그램과 연결

아래 주소는 실제 Worker 주소로 바꿉니다.

```powershell
$feedbackUrl = "https://privacy-news-feedback.계정.workers.dev"
[Environment]::SetEnvironmentVariable("FEEDBACK_API_URL", $feedbackUrl, "User")
$env:FEEDBACK_API_URL = $feedbackUrl
```

GitHub 저장소에서도 `Settings` → `Secrets and variables` → `Actions` →
`Variables` → `New repository variable`로 이동해 이름 `FEEDBACK_API_URL`, 값은
같은 Worker 주소를 저장합니다. 비밀 키가 아니므로 Secret이 아닌 Variable입니다.

그 후 Actions의 **Update News Report**를 수동 실행하거나 다음 예약 실행을 기다립니다.
Teams의 평가 링크는 웹 보고서로 이동해 확인을 받은 뒤 저장됩니다.

## 동작 기준

- 같은 브라우저가 같은 기사를 다시 평가하면 기존 평가가 갱신됩니다.
- 👎 한 번은 해당 기사만 제외합니다.
- 유사한 기사 유형의 👎가 2회 이상 누적되면 그 유형을 제외합니다.
- `신뢰도가 낮은 매체`가 같은 출처에 2회 이상 누적되면 그 출처를 제외합니다.
- 👍는 같은 주제·유사 기사와 해당 기사의 다음 선별 점수를 높입니다.
- API 장애 시 뉴스 작업은 중단하지 않고 피드백 반영만 건너뜁니다.
