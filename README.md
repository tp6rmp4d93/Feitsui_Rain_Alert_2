# 翡翠水庫集水區雨量警戒 LINE 通知

每 10 分鐘由 GitHub Actions 讀取[翡翠水庫即時水情資料（JSON 系統介接）](https://w3.feitsui.gov.tw/FeitsuiWebService/tpe/Feitsui_HourlyJSON.ashx)，任一雨量站或集水區平均達 **170 mm / 24 小時** 或 **70 mm / 3 小時** 時，以 LINE Bot 推播通知。

適用方案：Public repository + GitHub Free（公開 repo 的 Actions 分鐘數免費）。

```
GitHub Actions（排程）──讀 JSON、累計雨量、比對警戒值──▶ LINE Messaging API ──▶ 你的 LINE
Cloudflare Worker（Webhook，選用）◀── LINE 平台：只用來查 userId / groupId
```

## 雨量如何計算

JSON 是逐時資料，程式把每小時、每個雨量欄位的數值存進 `state.json`（保留近 30 小時），再加總出 3 小時與 24 小時累積。

- JSON 一次回傳多個小時時，會全部併入，啟用後立刻有完整累積值。
- JSON 只回傳最新一小時時，要等程式自己累積：**啟用後前 3 / 24 小時的累積值會偏低**。通知訊息會標示「僅累計到 N/24 小時」。
- 監測中斷超過一小時（排程被跳過、來源異常）會漏掉那幾個小時，累積值同樣偏低。
- 欄位名稱含「雨」或 `rain` 的數值欄位都視為一個測站；JSON 若有集水區平均欄位會一併納入，沒有的話就只比對各站。
- 若 JSON 已直接提供「3小時」「24小時」累積欄位，則優先採用。
- 雨量欄位若是「當日累積」而非「該小時雨量」，請把 Variable `RAIN_MODE` 設為 `cumulative`，程式會改用前後兩筆相減。

## 一、機密放在哪裡

| 項目 | 存放位置 | 絕對不可以出現在 |
|---|---|---|
| Channel access token | GitHub Actions Secret、Cloudflare Worker Secret | 程式碼、commit、Issue、Actions log |
| Channel secret | 只放 Cloudflare Worker Secret（GitHub 完全不需要它） | 同上 |
| userId / groupId（`LINE_TO`） | GitHub Actions Secret | 同上 |

repo 內的檔案（程式、workflow、`worker.js`）都不含任何機密，可以安心公開。

## 二、建立 LINE Bot

1. 登入 [LINE Developers Console](https://developers.line.biz/console/)，建立 Provider 與 Messaging API channel。
2. **Basic settings** 分頁：記下 *Channel secret*（Webhook 才會用到）。
3. **Messaging API** 分頁：最下方 *Channel access token (long-lived)* 按 Issue，記下 token。
4. 用手機掃同一頁的 QR code，把 Bot 加為好友。

## 三、建立 GitHub repo 並設定 Secrets

1. 建立 Public repo，把本資料夾全部內容 push 上去（`.gitignore` 已排除 `.env` 與 `state.json`）。
2. **Settings → Secrets and variables → Actions → New repository secret**，新增：
   - `LINE_CHANNEL_ACCESS_TOKEN`：步驟二的 token
   - `LINE_TO`：接收者的 userId 或 groupId，多個用逗號分隔。**不建立此 Secret = broadcast 給所有好友**
3. **Settings → Actions → General**：
   - *Workflow permissions* 選 **Read repository contents and packages permissions**
   - *Fork pull request workflows* 選 **Require approval for all external contributors**
4. **Actions** 分頁 → *Feitsui rain alert* → **Run workflow**，依序驗證：
   - `dump`：log 會印出最新一筆原始 JSON 與各測站的計算結果。請核對測站是否齊全、數值是「時雨量」還是「當日累積」
   - `test-line`：LINE 應收到測試訊息
5. 之後排程會自動每 10 分鐘以 `once` 模式執行。

選用的調整值放在同頁的 **Variables** 分頁（非機密）：`RENOTIFY_MINUTES`、`VERIFY_SSL`、`RAIN_MODE`、`RAIN_KEY_PATTERN`。

## 四、Webhook 設定

**先確認是否需要：** 警戒通知是單向推播，不需要 Webhook。只有要取得 groupId（發到群組）或他人的 userId 時才需要。只通知自己的話，Console 的 Basic settings 最下方 *Your user ID* 就是你的 userId；用 broadcast 則連 ID 都不用。

GitHub Actions 無法接收外部連入，所以 Webhook 放在 Cloudflare Workers（免費方案足夠）。

### 4-1 部署 Worker

1. 登入 [Cloudflare Dashboard](https://dash.cloudflare.com/) → **Workers & Pages → Create → Worker**（Hello World 範本）→ Deploy。
2. **Edit code**，把內容整個換成 `webhook/worker.js`，再 Deploy。
3. Worker 的 **Settings → Variables and Secrets → Add**，類型選 **Secret**：
   - `LINE_CHANNEL_SECRET`
   - `LINE_CHANNEL_ACCESS_TOKEN`
4. 記下 Worker 網址，例如 `https://line-webhook.<你的子網域>.workers.dev`。

### 4-2 在 LINE Console 啟用

1. **Messaging API** 分頁 → *Webhook URL* 貼上 Worker 網址 → **Update**。
2. 按 **Verify**，應顯示 Success。失敗多半是 `LINE_CHANNEL_SECRET` 填錯。
3. 打開 **Use webhook**。
4. 同頁 *LINE Official Account features*：
   - *Allow bot to join group chats* → Enabled（要發到群組才需要）
   - *Auto-reply messages* → Disabled（否則每則訊息都會收到罐頭回覆）

### 4-3 取得 ID

- 個人：在與 Bot 的一對一聊天輸入 `/id`。
- 群組：把 Bot 邀進群組，在群組輸入 `/id`，回覆中的 `groupId`（`C` 開頭）就是群組 ID。

把 ID 填入 GitHub Secret `LINE_TO`。

### 4-4 用完後收尾（擇一）

- 在 LINE Console 關閉 **Use webhook**；或
- 在 Worker 新增 Secret `ADMIN_USER_ID`（填你自己的 userId），之後只有你下的 `/id` 會被回應。

### Webhook 如何防止偽造

LINE 每次呼叫都會附上 `X-Line-Signature` 標頭，內容是以 Channel secret 對請求本文做 HMAC-SHA256 的結果。`worker.js` 會自行重算並比對，不符即回 401，因此就算別人知道 Worker 網址也無法假冒 LINE。

## 五、帳號與金鑰防護清單

- [ ] GitHub 帳號啟用兩步驟驗證（Settings → Password and authentication）
- [ ] LINE 帳號設定密碼並保護好登入用的 Email；Cloudflare 帳號啟用兩步驟驗證
- [ ] token 只貼進 Secret 欄位，不貼在 Issue、PR、聊天室或截圖中
- [ ] 本機測試用的 `.env` 不 commit（已在 `.gitignore`）
- [ ] 不在 workflow 加入 `pull_request_target` 觸發，不在步驟中 `echo` Secrets
- [ ] 不隨意新增 Collaborator：具寫入權限的人可以修改 workflow 並取用 Secrets
- [ ] 進階：把 workflow 裡的 `actions/checkout@v4` 等改為釘選完整 commit SHA，防範 Action 被竄改
- [ ] Settings → Code security 確認 Secret scanning 與 Push protection 已開啟（多一層保險，但不要依賴它）

**懷疑外洩時：** 到 LINE Console 的 Messaging API 分頁對 token 按 **Reissue**（舊 token 立即失效），Channel secret 在 Basic settings 按 **Issue** 重發，再更新 GitHub 與 Cloudflare 的 Secret。

## 六、此方案的限制

- **不是即時**：GitHub 排程在尖峰時段可能延遲數分鐘到數十分鐘，偶爾會跳過一次。
- **排程會被自動停用**：公開 repo 連續 60 天沒有任何活動時，GitHub 會停用排程並寄信通知，需到 Actions 分頁手動重新啟用。
- **Actions log 是公開的**：程式已避免在 log 印出 token 與接收者 ID；自行修改程式時請維持這一點。
- **來源網站可能擋國外 IP**：GitHub 的執行機在國外。若 `dump` 一直連線失敗但你在台灣開得了該網址，就是被擋，需改用台灣的主機執行（程式不帶參數即為常駐模式）。
- **LINE 免費訊息則數有限**：broadcast 是每位好友各算一則。Webhook 的回覆訊息不計入額度。

## 七、本機測試

```bash
pip install -r requirements.txt
cp .env.example .env            # 填入 token
python feitsui_rain_alert.py --dump
python feitsui_rain_alert.py --test-line
python feitsui_rain_alert.py --dump --file sample.json   # 用另存的 JSON 檔測試
```
