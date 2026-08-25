import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Dispatch",
  description: "Call a number. Watch Chrome do the thing.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
