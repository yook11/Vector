import type { MetadataRoute } from "next";

// PWA マニフェスト。アイコン背景がティール (#0FA89C) なので splash / theme も合わせる。
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Vector — Tech News & AI Analysis",
    short_name: "Vector",
    description: "海外テックニュース収集・AI翻訳・投資分析ダッシュボード",
    start_url: "/",
    display: "standalone",
    background_color: "#0FA89C",
    theme_color: "#0FA89C",
    icons: [
      {
        src: "/icons/icon-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icons/icon-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "any",
      },
      {
        // フルブリード正方形。中央寄せで safe-zone を満たすため maskable に流用
        src: "/icons/icon-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
  };
}
