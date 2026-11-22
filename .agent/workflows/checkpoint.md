---
description: 建立安全回朔點（備份資料庫、檢查隱私檔案、Git Commit 並打上 Tag 推送）
---
請在 `/verify` 全數通過後，依序執行以下回朔點建立步驟：

1. **備份資料庫**：執行 `python scripts/backup_db.py`，確保目前的 `sports_travel.db` 狀態已存檔至 `backups/`。
2. **安全檢查**：執行 `git status`，嚴格檢查 `.env`、`.venv/`、`*.db`、`backups/`、`__pycache__/` 是否皆已被 `.gitignore` 正確忽略。若發現任何敏感檔案（特別是 `.env`）出現在待提交清單中，**立即停止並修正 `.gitignore`**。
3. **詢問或確認版本資訊**：檢查目前的開發進度，決定適當的 Commit 訊息（例如 `feat: complete phase X ...`）與 Tag 名稱（例如 `v0.X-phaseX`）。
4. **執行 Git 存檔與推送**：
   - `git add .`
   - `git commit -m "<commit訊息>"`
   - `git tag <tag名稱>`
   - `git push origin main --tags`
5. **回報結果**：向我報告本次存檔的 Commit Hash、Tag 名稱，以及若未來需要退回此版本時可使用的 `git reset --hard <tag名稱>` 指令。