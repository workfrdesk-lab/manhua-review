import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Recap Studio — حالة النظام",
  description: "مساحة عمل لإنشاء فيديوهات سرد المانهوا من محتوى تملك حقوق استخدامه.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="ar" dir="rtl"><body>{children}</body></html>;
}