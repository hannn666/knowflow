import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title: "KnowFlow · 知识库工作台",
  description: "你的文档，你的知识空间",
};
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
