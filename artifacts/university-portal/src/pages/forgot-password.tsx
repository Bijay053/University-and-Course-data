import { useState, FormEvent } from "react";
import { Link } from "wouter";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import brandLogo from "@assets/image_1776917782083.png";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

export default function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);
  const [debugUrl, setDebugUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await fetch(`${BASE}/api/auth/forgot-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data?.detail || "Request failed");
      }
      setDone(true);
      setDebugUrl(typeof data.debug_reset_url === "string" ? data.debug_reset_url : null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-[100dvh] flex items-center justify-center bg-muted/40 px-4">
      <Card className="w-full max-w-sm shadow-lg">
        <CardHeader className="text-center space-y-3">
          <div className="flex justify-center">
            <img src={brandLogo} alt="Study Info Centre" className="h-12 w-auto" />
          </div>
          <div>
            <CardTitle className="text-xl">Forgot password</CardTitle>
            <CardDescription className="text-sm mt-1">
              Enter your account email and we'll send a reset link.
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          {done ? (
            <div className="space-y-4 text-sm">
              <div className="rounded-md bg-emerald-50 text-emerald-900 px-3 py-3 border border-emerald-200">
                If an account exists for that email, a reset link is on its way.
                Check your inbox (and spam folder).
              </div>
              {debugUrl && (
                <div className="rounded-md bg-amber-50 text-amber-900 px-3 py-3 border border-amber-200 break-all">
                  <p className="font-medium mb-1">Dev mode (SMTP not configured):</p>
                  <a className="underline text-primary" href={debugUrl}>{debugUrl}</a>
                </div>
              )}
              <Link href="/login" className="text-primary hover:underline block text-center">
                ← Back to sign in
              </Link>
            </div>
          ) : (
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="email">Email</Label>
                <Input
                  id="email"
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                />
              </div>
              {error && (
                <p className="text-sm text-destructive bg-destructive/10 rounded-md px-3 py-2">
                  {error}
                </p>
              )}
              <Button type="submit" className="w-full" disabled={loading}>
                {loading ? "Sending…" : "Send reset link"}
              </Button>
              <Link href="/login" className="text-xs text-muted-foreground hover:underline block text-center">
                ← Back to sign in
              </Link>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
