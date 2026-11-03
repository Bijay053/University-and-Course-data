import { useEffect, useState, FormEvent } from "react";
import { Link, useLocation } from "wouter";
import { Eye, EyeOff } from "lucide-react";
import { useAuth, type LoginChallenge } from "@/context/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";

export default function Login() {
  const { login, verifyOtp } = useAuth();
  const [, navigate] = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [challenge, setChallenge] = useState<LoginChallenge | null>(null);
  const [code, setCode] = useState("");
  const [resendWait, setResendWait] = useState(0);

  useEffect(() => {
    if (!challenge) return;
    const timer = window.setInterval(() => setResendWait((value) => Math.max(0, value - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [challenge]);

  async function sendCode() {
    const next = await login(email, password, "email-otp-v1");
    setChallenge(next);
    setResendWait(next.resend_after);
    setCode("");
  }

  async function resendCode() {
    setError(null);
    setLoading(true);
    try {
      await sendCode();
    } catch (err) {
      setError((err as Error).message || "Could not resend your code.");
    } finally {
      setLoading(false);
    }
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      if (challenge) {
        await verifyOtp(challenge.challenge_token, code);
        setPassword("");
        navigate("/");
      } else {
        await sendCode();
      }
    } catch (err) {
      setError((err as Error).message || "Login failed. Check your credentials.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-[100dvh] flex items-center justify-center bg-muted/40 px-4">
      <Card className="w-full max-w-sm shadow-lg">
        <CardHeader className="text-center space-y-3">
          <div className="flex justify-center">
            <div
              aria-label="Study Info Centre"
              className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary text-lg font-bold text-primary-foreground"
            >
              SI
            </div>
          </div>
          <div>
            <CardTitle className="text-xl">Study Info Centre</CardTitle>
            <CardDescription className="text-sm mt-1">
              {challenge ? `Enter the code sent to ${challenge.masked_email}` : "Admin Portal — Email verification required"}
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            {!challenge && <>
            <div className="space-y-1.5">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
              />
            </div>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label htmlFor="password">Password</Label>
                <Link
                  href="/forgot-password"
                  className="text-xs text-primary hover:underline"
                >
                  Forgot password?
                </Link>
              </div>
              <div className="relative">
                <Input
                  id="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  className="pr-10"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  tabIndex={-1}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                  className="absolute inset-y-0 right-0 flex items-center px-3 text-muted-foreground hover:text-foreground"
                >
                  {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>
            </>}
            {challenge && <div className="space-y-1.5">
              <Label htmlFor="verification-code">Email verification code</Label>
              <Input
                id="verification-code" inputMode="numeric" autoComplete="one-time-code"
                pattern="[0-9]{6}" maxLength={6} minLength={6} required autoFocus
                value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))}
                placeholder="6-digit code" className="text-center text-lg tracking-[0.25em]"
              />
              <p className="text-xs text-muted-foreground">Use the most recent code in your inbox. Codes expire after 10 minutes.</p>
            </div>}
            {error && (
              <p role="alert" className="text-sm text-destructive bg-destructive/10 rounded-md px-3 py-2">
                {error}
              </p>
            )}
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? "Please wait…" : challenge ? "Verify and sign in" : "Send verification code"}
            </Button>
            {challenge && <div className="space-y-2 text-center">
              <Button type="button" variant="outline" className="w-full" disabled={loading || resendWait > 0} onClick={resendCode}>
                {resendWait > 0 ? `Resend code in ${resendWait}s` : "Resend code"}
              </Button>
              <button type="button" disabled={loading} className="text-sm text-primary hover:underline"
                onClick={() => { setChallenge(null); setCode(""); setPassword(""); setError(null); }}>
                Use another account
              </button>
            </div>}
          </form>
          <p className="mt-5 border-t pt-4 text-center text-sm text-muted-foreground">
            Looking for a course?{" "}
            <Link href="/toolkit" className="font-medium text-primary hover:underline" data-testid="link-browse-courses">
              Browse courses
            </Link>
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
