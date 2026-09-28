# jobseekers-mcp

[English](README.md) | **繁體中文**

[Jobseekers](https://github.com/steven91007/Jobseekers-) 的 [MCP](https://modelcontextprotocol.io/) server：讓 Claude Code 之類的 agent 直接呼叫 LinkedIn 職缺搜尋、簽證支持判斷、git 歷史知識庫（gitkb）與 Discord bot 狀態，每次呼叫都可追蹤到 [Langfuse](https://langfuse.com/)。

這個 repo 只放 MCP 這一層。實際的爬蟲、簽證規則、gitkb 與 bot 資料庫程式碼都在 Jobseekers 專案裡，server 會直接 import 它們，不另外複製一份，所以兩邊的修正永遠同步。

## 工具

| 工具 | 作用 |
|---|---|
| `search_jobs` | 搜尋 LinkedIn 職缺（最新優先，可用多地點、地區預設與 `posted_within` 24h／7d／30d）。回傳 `outcome`，讓 agent 分辨「沒有職缺」和「被封鎖／爬蟲壞了」 |
| `get_job_detail` | 讀單一職缺的完整描述、條件，以及規則判斷的簽證結果 |
| `check_visa` | 一次檢查最多 15 筆職缺是否提供簽證支持。規則判不出來的會附上描述，**交給呼叫端的模型自己判斷**，所以 server 不需要任何 LLM 金鑰 |
| `gitkb_search` / `gitkb_show` / `gitkb_log` / `gitkb_history` | 查詢 git 歷史知識庫：改程式前先查「為什麼當初這樣寫」 |
| `gitkb_pending` / `gitkb_import_summaries` | 讓 agent 為還沒摘要的 commit 撰寫並匯入摘要 |
| `list_subscriptions` / `bot_status` | Discord bot 的訂閱與上次推播狀態（唯讀，以 `mode=ro` 開啟資料庫） |

另外提供 resource `jobs://regions`（地區預設清單）和 prompt `gitkb_update`（更新知識庫的步驟）。

所有 LinkedIn 工具共用一個節流器（`MCP_LINKEDIN_MIN_GAP`，預設 3 秒），`search_jobs` 最多 50 筆、`check_visa` 最多 15 筆。被封鎖時工具會回傳錯誤，並明確告訴 agent 不要重試。

## 它怎麼找到 Jobseekers 專案

依序嘗試以下位置，找到含有 `linkedin_scraper.py`、`visa.py`、`gitkb/`、`bot/` 的目錄就停：

1. 環境變數 `JOBSEEKERS_ROOT`
2. 這個 repo 的上一層：以 submodule 掛在 Jobseekers 裡時，就是 Jobseekers 本身
3. 目前工作目錄及其上層目錄（MCP client 通常在專案目錄裡啟動 server）

`--check` 會印出實際使用的路徑。

## 使用方式

### 在 Jobseekers 裡（submodule，建議）

Jobseekers 已經把這個 repo 以 submodule 掛在 `jobseekers-mcp/`，並在 `.mcp.json` 登記好 server：

```bash
git clone --recurse-submodules https://github.com/steven91007/Jobseekers-.git
# 已經 clone 過的話：
git submodule update --init
```

在 Jobseekers 目錄開 Claude Code，第一次會詢問是否啟用 `jobseekers` server，同意即可。它實際執行的指令是：

```bash
uv run --no-project --python 3.13 --with-editable ./jobseekers-mcp python -m mcp_server
```

### 單獨使用

```bash
git clone https://github.com/steven91007/jobseekers-mcp.git
cd jobseekers-mcp
uv venv --python 3.13 && uv pip install -e .
JOBSEEKERS_ROOT=/path/to/Jobseekers- .venv/bin/jobseekers-mcp --check
```

在其他 MCP client 中登記時，command 填 `jobseekers-mcp`（或 `python -m mcp_server`），並在 env 帶上 `JOBSEEKERS_ROOT`。

## 設定

設定從 Jobseekers 專案的 `.env` 讀取；也可以用 `JOBSEEKERS_ENV_FILE` 指定其他檔案。以下都是選填：

| 變數 | 說明 |
|---|---|
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | 兩把金鑰都有才會追蹤；沒有的話 server 照常運作 |
| `LANGFUSE_TRACING_ENVIRONMENT` | `production`（預設）或 `development`，測試時避免污染正式的 dashboard |
| `JOBAGENT_USER_ID` | trace 上的 user id，預設 `me` |
| `MCP_LANGFUSE_MASK` | 設 `0` 可關閉 email／電話／金鑰遮罩（預設開啟） |
| `MCP_LINKEDIN_MIN_GAP` | 兩次 LinkedIn 工具呼叫之間的最小秒數，預設 3 |
| `MCP_SESSION_ID` | 覆寫 Langfuse session id（預設每個 server 行程一個） |
| `GITKB_REPO` / `JOBBOT_DB` | gitkb 要讀的 repo／bot 資料庫路徑，預設是 Jobseekers 專案 |

## Langfuse 追蹤

- **一次工具呼叫就是一個 trace**。根 observation 的 input 是工具參數、output 是工具結果，名稱固定（`search-linkedin-jobs`、`check-visa-sponsorship`、`search-git-history`……，完整清單見 `mcp_server/observability.py` 的 `NAMES`），可以直接拿來建 dashboard 或 evaluator。
- `check_visa` 底下每筆職缺各有一個 `check-job-visa`，裡面再分成 `fetch-job-description`（retriever）與 `classify-visa-rules`。
- 同一個 server 行程的所有 trace 共用一個 session id，tag 為 `mcp` 加上 `jobs`、`gitkb` 或 `bot`。
- MCP client 若在請求的 `_meta` 帶了 W3C `traceparent`，工具的 trace 會直接接到 client 的 trace 底下。MCP SDK 自己產生的 `tools/call` span 不會送出。
- 職缺描述裡的 email、電話與 API 金鑰在送出前就會被遮罩。
- 每次工具呼叫結束、以及收到 SIGTERM 時都會立刻 flush。MCP client 關閉 session 時常直接結束 server 行程，不這樣做的話最後幾筆 trace 會遺失。

## 開發

```bash
uv venv --python 3.13 && uv pip install -e ".[test]"
JOBSEEKERS_ROOT=../Jobseekers- .venv/bin/python -m pytest -q
```

測試完全離線：LinkedIn 用假的回應，Langfuse 的 span 導到記憶體裡，用來檢查 trace 的巢狀結構、類型、session 與遮罩；gitkb 在暫存 git repo 裡跑完整的 pending／import 流程。CI（`.github/workflows/ci.yml`）會 checkout Jobseekers 的 master 來跑同一套測試。

### 發版與更新 Jobseekers 裡的版本

1. 在這個 repo 改好、推上 `main`，確認 CI 通過；需要固定版本時打 tag（例如 `v1.1.0`），並同步更新 `pyproject.toml` 與 `mcp_server/__init__.py` 的版本號。
2. 在 Jobseekers 裡把 submodule 指到新的 commit：

   ```bash
   git submodule update --remote jobseekers-mcp   # 或 cd jobseekers-mcp && git checkout v1.1.0
   git add jobseekers-mcp && git commit -m "Bump jobseekers-mcp to v1.1.0"
   ```

如果改動需要 Jobseekers 核心模組配合修改，先合併 Jobseekers 那邊的變更，再更新這裡。
