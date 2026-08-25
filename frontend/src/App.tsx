import { Compass, Network } from "lucide-react"

function App() {
  return (
    <>
      <a className="skip-link" href="#workspace">
        Đi đến bàn điều phối
      </a>
      <main
        id="workspace"
        className="grid min-h-svh place-items-center px-6 py-12"
      >
        <section className="atlas-panel atlas-elevated w-full max-w-xl p-8 sm:p-10">
          <div className="mb-8 flex items-center justify-between border-b pb-4">
            <span className="flex items-center gap-2 font-semibold">
              <Compass aria-hidden="true" className="size-5 text-accent" />
              Thương Trí
            </span>
            <span className="atlas-data text-muted-foreground">ATLAS / 01</span>
          </div>
          <Network aria-hidden="true" className="mb-5 size-8 text-primary" />
          <h1 className="max-w-md font-display text-4xl leading-tight font-semibold tracking-[-0.025em] text-balance sm:text-5xl">
            Bàn điều phối quyết định có thể kiểm chứng.
          </h1>
          <p className="mt-5 max-w-lg leading-7 text-muted-foreground">
            Nền tảng giao diện đã sẵn sàng. Không gian hội thoại, tuyến thực thi
            và chỉ mục bằng chứng sẽ được kết nối ở lát cắt tiếp theo.
          </p>
        </section>
      </main>
    </>
  )
}

export default App
