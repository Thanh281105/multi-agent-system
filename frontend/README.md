# Evidence Atlas frontend

React/TypeScript workspace cho giao diện quyết định dựa trên snapshot lịch sử
Tiki Books. Vite xuất production bundle vào `app/frontend/dist`; FastAPI phục vụ
thư mục đó cùng origin với API. Mọi giá, rating, review và thống kê hiển thị đều
là evidence lịch sử, không phải catalog hay thị trường Tiki hiện tại.

## Local workflow

```powershell
npm ci
npm run dev
```

The development server proxies `/api` to `http://127.0.0.1:8000`.

## Required checks

```powershell
npm run lint
npm test
npm run build
```

Do not hand-edit files under `app/frontend/dist`. Keep UI decisions aligned
with the root `DESIGN.md`. API keys are runtime input and must never be stored
in source, browser storage, logs, screenshots, or generated artifacts.
