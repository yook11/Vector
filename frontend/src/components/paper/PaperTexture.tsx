function grainDataUri(frequency: string, octaves: number): string {
  const svg =
    "<svg xmlns='http://www.w3.org/2000/svg' width='220' height='220'>" +
    `<filter id='n'><feTurbulence type='fractalNoise' baseFrequency='${frequency}' numOctaves='${octaves}' stitchTiles='stitch'/>` +
    "<feColorMatrix type='saturate' values='0'/></filter>" +
    "<rect width='100%' height='100%' filter='url(#n)'/></svg>";
  return `url("data:image/svg+xml;utf8,${encodeURIComponent(svg)}")`;
}

const GRAIN_FINE = grainDataUri("0.8", 2);
const GRAIN_FIBER = grainDataUri("0.012 0.16", 1);

export function PaperTexture() {
  return (
    <>
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 z-0 opacity-[0.045] mix-blend-multiply dark:opacity-[0.055]"
        style={{
          backgroundImage: GRAIN_FINE,
          backgroundSize: "220px 220px",
        }}
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 z-0 opacity-[0.03] mix-blend-screen dark:opacity-[0.04]"
        style={{
          backgroundImage: GRAIN_FINE,
          backgroundSize: "220px 220px",
        }}
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 z-0 opacity-[0.028] mix-blend-soft-light dark:opacity-[0.04]"
        style={{
          backgroundImage: GRAIN_FIBER,
          backgroundSize: "320px 320px",
        }}
      />
    </>
  );
}
