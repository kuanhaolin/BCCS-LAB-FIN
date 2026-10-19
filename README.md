# 表單工作台

在本機整合公差申請、主計室報帳、計畫文件列印及主持人費撥帳。操作會連到校方系統。按下儲存、存入、送出或送審前，請確認資料與案件清單，避免重複建立。

## 啟動

在本目錄執行：

```bash
./web
```

也可以執行 `python3 ncu_form_app.py`。接著在同一台電腦開啟 [http://127.0.0.1:1234/](http://127.0.0.1:1234/)。服務只監聽本機，不要用反向代理或路由器將它公開，因為網頁會接收登入 Cookie 和表單資料。

Portal Session 供 HumanSys、ResearchSys 與撥帳系統共用，儲存在 `session.json`，主計室報帳使用獨立的 `session01.json`。各頁都有手動更新及自動更新Session。可參照 `.env_example` 建立私人 `.env`：自動更新 Portal 需設定 `PORTAL_ID`、`PORTAL_PW`，並備妥專用 Chrome、Selenium 與 Buster；自動更新報帳需設定 `ACCOUNT_ID`、`ACCOUNT_PW`。驗證碼或校方登入失敗時，不會用未經確認的新 Session 取代原資料。

## 1. 出差申請

## 2. 出差報帳

## 3. 查詢計劃（列印文件）

## 4. 主持人費報帳

## 5. 主持人費撥帳

## 檔案配置

- `ncu_form_app.py`、`web`：本機網頁入口與啟動腳本。
- `humansys_*.py`、`account_*.py`、`research_*.py`、`appropriation_*.py`：各校內系統的連線、驗證、儲存與列印程式。
- `input.json`、`input01.json`、`input02.json`、`input03.json`：上述四種填寫流程的預設資料，計畫檢視沒有填寫預設檔。
- `input_example.json`、`input01_example.json`、`input02_example.json`、`input03_example.json`：可公開的欄位範例，不含真實個資。新環境可參照它們建立對應的私人 `input*.json`，填入自己的資料，不要覆蓋本機已存在的私人預設檔。範例含虛構代碼及未填的身分欄位，不能直接送出。
- `session.json`、`session01.json`、`.env`、`p@ssw0rd.json`：本機私有資料，勿分享或提交至公開儲存庫。
- `.env_example`：可公開的空白環境變數範例；不要在此檔填入真實帳密。
- `prints/`：產生的 PDF；`tests/`：自動化測試，日常使用網頁不必執行。
