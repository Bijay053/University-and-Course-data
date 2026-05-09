import { useEffect, useMemo, useState, FormEvent } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, KeyRound, ShieldCheck, ShieldOff, Eye, EyeOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Switch } from "@/components/ui/switch";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { useToast } from "@/hooks/use-toast";
import { useAuth } from "@/context/auth";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

interface UserRow {
  id: number;
  email: string;
  full_name: string;
  is_active: boolean;
  is_super_admin: boolean;
}

interface RegistryGroup {
  group: string;
  permissions: { key: string; label: string }[];
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { credentials: "include", ...init });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

export default function UsersPage() {
  const { user: me } = useAuth();
  const { toast } = useToast();
  const qc = useQueryClient();

  const usersQ = useQuery({
    queryKey: ["users"],
    queryFn: () => api<UserRow[]>("/api/users"),
  });
  const registryQ = useQuery({
    queryKey: ["permissions-registry"],
    queryFn: () => api<RegistryGroup[]>("/api/permissions/registry"),
  });

  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<UserRow | null>(null);
  const [permsFor, setPermsFor] = useState<UserRow | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<UserRow | null>(null);

  const refresh = () => qc.invalidateQueries({ queryKey: ["users"] });

  return (
    <div className="container mx-auto p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Users & Permissions</h1>
          <p className="text-sm text-muted-foreground">
            Add team members and toggle exactly which buttons and tabs each can use.
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4 mr-2" />
          Add user
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">All users</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {usersQ.isLoading ? (
            <p className="p-6 text-sm text-muted-foreground">Loading…</p>
          ) : usersQ.error ? (
            <p className="p-6 text-sm text-destructive">{(usersQ.error as Error).message}</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b text-xs uppercase text-muted-foreground bg-muted/40">
                  <tr>
                    <th className="text-left px-4 py-2 font-medium">Email</th>
                    <th className="text-left px-4 py-2 font-medium">Name</th>
                    <th className="text-left px-4 py-2 font-medium">Status</th>
                    <th className="text-left px-4 py-2 font-medium">Role</th>
                    <th className="text-right px-4 py-2 font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {(usersQ.data ?? []).map((u) => (
                    <tr key={u.id} className="border-b last:border-0 hover:bg-muted/20">
                      <td className="px-4 py-3 font-mono text-xs">{u.email}</td>
                      <td className="px-4 py-3">{u.full_name || "—"}</td>
                      <td className="px-4 py-3">
                        {u.is_active ? (
                          <Badge variant="default" className="bg-emerald-600">Active</Badge>
                        ) : (
                          <Badge variant="secondary">Disabled</Badge>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {u.is_super_admin ? (
                          <Badge variant="default" className="bg-violet-600">
                            <ShieldCheck className="h-3 w-3 mr-1" /> Super admin
                          </Badge>
                        ) : (
                          <Badge variant="outline">User</Badge>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center justify-end gap-2">
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => setPermsFor(u)}
                            disabled={u.is_super_admin}
                            title={u.is_super_admin ? "Super admin has all permissions" : "Edit permissions"}
                          >
                            <KeyRound className="h-3.5 w-3.5 mr-1" />
                            Permissions
                          </Button>
                          <Button size="sm" variant="outline" onClick={() => setEditing(u)}>
                            Edit
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            className="text-destructive hover:bg-destructive/10"
                            onClick={() => setConfirmDelete(u)}
                            disabled={u.id === me?.id}
                            title={u.id === me?.id ? "You cannot delete your own account" : "Delete user"}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <CreateUserDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        registry={registryQ.data ?? []}
        onCreated={() => {
          refresh();
          toast({ title: "User created" });
        }}
      />

      <EditUserDialog
        user={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          refresh();
          toast({ title: "User updated" });
        }}
        currentUserId={me?.id}
      />

      <PermissionsDialog
        user={permsFor}
        registry={registryQ.data ?? []}
        onClose={() => setPermsFor(null)}
        onSaved={() => toast({ title: "Permissions updated" })}
      />

      <AlertDialog
        open={!!confirmDelete}
        onOpenChange={(o) => !o && setConfirmDelete(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete user?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently removes <strong>{confirmDelete?.email}</strong> and revokes
              all their permissions. This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={async () => {
                if (!confirmDelete) return;
                try {
                  await api(`/api/users/${confirmDelete.id}`, { method: "DELETE" });
                  refresh();
                  toast({ title: "User deleted" });
                } catch (err) {
                  toast({
                    title: "Delete failed",
                    description: (err as Error).message,
                    variant: "destructive",
                  });
                } finally {
                  setConfirmDelete(null);
                }
              }}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-dialogs
// ---------------------------------------------------------------------------

function CreateUserDialog({
  open,
  onOpenChange,
  registry,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  registry: RegistryGroup[];
  onCreated: () => void;
}) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [isSuper, setIsSuper] = useState(false);
  const [perms, setPerms] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) {
      setEmail("");
      setName("");
      setPassword("");
      setIsSuper(false);
      setPerms(new Set());
      setError(null);
      setShowPw(false);
    }
  }, [open]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setLoading(true);
    try {
      await api("/api/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email,
          full_name: name,
          password,
          is_super_admin: isSuper,
          permissions: Array.from(perms),
        }),
      });
      onCreated();
      onOpenChange(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Add user</DialogTitle>
          <DialogDescription>
            Create a new account. Pick the buttons and tabs they should see.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="email">Email</Label>
              <Input id="email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="name">Full name</Label>
              <Input id="name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="pw">Password (min 8 characters)</Label>
            <div className="relative">
              <Input
                id="pw"
                type={showPw ? "text" : "password"}
                required
                minLength={8}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="pr-10"
              />
              <button
                type="button"
                onClick={() => setShowPw((v) => !v)}
                tabIndex={-1}
                className="absolute inset-y-0 right-0 flex items-center px-3 text-muted-foreground hover:text-foreground"
              >
                {showPw ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </div>
          <div className="flex items-center justify-between rounded-md border p-3 bg-muted/30">
            <div>
              <p className="text-sm font-medium">Super admin</p>
              <p className="text-xs text-muted-foreground">Has every permission and can manage other users.</p>
            </div>
            <Switch checked={isSuper} onCheckedChange={setIsSuper} />
          </div>
          {!isSuper && (
            <div className="space-y-2">
              <Label>Permissions</Label>
              <PermissionsGrid
                registry={registry}
                selected={perms}
                onChange={setPerms}
              />
            </div>
          )}
          {error && (
            <p className="text-sm text-destructive bg-destructive/10 rounded-md px-3 py-2">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={loading}>
              {loading ? "Creating…" : "Create user"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function EditUserDialog({
  user,
  onClose,
  onSaved,
  currentUserId,
}: {
  user: UserRow | null;
  onClose: () => void;
  onSaved: () => void;
  currentUserId?: number;
}) {
  const [name, setName] = useState("");
  const [active, setActive] = useState(true);
  const [isSuper, setIsSuper] = useState(false);
  const [newPw, setNewPw] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (user) {
      setName(user.full_name);
      setActive(user.is_active);
      setIsSuper(user.is_super_admin);
      setNewPw("");
      setError(null);
      setShowPw(false);
    }
  }, [user]);

  if (!user) return null;
  const isSelf = user.id === currentUserId;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!user) return;
    setLoading(true);
    try {
      const body: Record<string, unknown> = {
        full_name: name,
        is_active: active,
        is_super_admin: isSuper,
      };
      if (newPw) {
        if (newPw.length < 8) throw new Error("New password must be at least 8 characters.");
        body.new_password = newPw;
      }
      await api(`/api/users/${user.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      onSaved();
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={!!user} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit user</DialogTitle>
          <DialogDescription className="font-mono text-xs">{user.email}</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="ename">Full name</Label>
            <Input id="ename" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="flex items-center justify-between rounded-md border p-3">
            <div>
              <p className="text-sm font-medium">Active</p>
              <p className="text-xs text-muted-foreground">Disabled accounts cannot sign in.</p>
            </div>
            <Switch checked={active} onCheckedChange={setActive} disabled={isSelf} />
          </div>
          <div className="flex items-center justify-between rounded-md border p-3">
            <div>
              <p className="text-sm font-medium flex items-center gap-1">
                {isSuper ? <ShieldCheck className="h-4 w-4 text-violet-600" /> : <ShieldOff className="h-4 w-4 text-muted-foreground" />}
                Super admin
              </p>
              <p className="text-xs text-muted-foreground">All permissions plus user management.</p>
            </div>
            <Switch checked={isSuper} onCheckedChange={setIsSuper} disabled={isSelf} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="newpw">New password (leave blank to keep)</Label>
            <div className="relative">
              <Input
                id="newpw"
                type={showPw ? "text" : "password"}
                value={newPw}
                onChange={(e) => setNewPw(e.target.value)}
                placeholder="Leave blank to keep current password"
                className="pr-10"
              />
              <button
                type="button"
                onClick={() => setShowPw((v) => !v)}
                tabIndex={-1}
                className="absolute inset-y-0 right-0 flex items-center px-3 text-muted-foreground hover:text-foreground"
              >
                {showPw ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </div>
          {error && (
            <p className="text-sm text-destructive bg-destructive/10 rounded-md px-3 py-2">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={loading}>
              {loading ? "Saving…" : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function PermissionsDialog({
  user,
  registry,
  onClose,
  onSaved,
}: {
  user: UserRow | null;
  registry: RegistryGroup[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    setLoading(true);
    api<{ permissions: string[] }>(`/api/users/${user.id}/permissions`)
      .then((d) => setSelected(new Set(d.permissions)))
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [user]);

  if (!user) return null;

  async function save() {
    if (!user) return;
    setSaving(true);
    setError(null);
    try {
      await api(`/api/users/${user.id}/permissions`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ permissions: Array.from(selected) }),
      });
      onSaved();
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={!!user} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Permissions</DialogTitle>
          <DialogDescription>
            Toggle exactly what <span className="font-mono">{user.email}</span> can do.
          </DialogDescription>
        </DialogHeader>
        {loading ? (
          <p className="text-sm text-muted-foreground py-6">Loading…</p>
        ) : (
          <PermissionsGrid registry={registry} selected={selected} onChange={setSelected} />
        )}
        {error && (
          <p className="text-sm text-destructive bg-destructive/10 rounded-md px-3 py-2">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={save} disabled={saving || loading}>
            {saving ? "Saving…" : "Save permissions"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PermissionsGrid({
  registry,
  selected,
  onChange,
}: {
  registry: RegistryGroup[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
}) {
  const allKeys = useMemo(
    () => registry.flatMap((g) => g.permissions.map((p) => p.key)),
    [registry],
  );
  const allChecked = allKeys.length > 0 && allKeys.every((k) => selected.has(k));
  const noneChecked = allKeys.every((k) => !selected.has(k));

  function toggle(key: string) {
    const next = new Set(selected);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onChange(next);
  }
  function toggleGroup(group: RegistryGroup) {
    const next = new Set(selected);
    const groupKeys = group.permissions.map((p) => p.key);
    const allOn = groupKeys.every((k) => next.has(k));
    if (allOn) groupKeys.forEach((k) => next.delete(k));
    else groupKeys.forEach((k) => next.add(k));
    onChange(next);
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-xs">
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => onChange(new Set(allKeys))}
          disabled={allChecked}
        >
          Grant all
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => onChange(new Set())}
          disabled={noneChecked}
        >
          Revoke all
        </Button>
        <span className="text-muted-foreground ml-auto">
          {selected.size} / {allKeys.length} granted
        </span>
      </div>
      <div className="space-y-3">
        {registry.map((group) => {
          const groupKeys = group.permissions.map((p) => p.key);
          const allOn = groupKeys.every((k) => selected.has(k));
          const someOn = groupKeys.some((k) => selected.has(k));
          return (
            <div key={group.group} className="rounded-md border">
              <button
                type="button"
                onClick={() => toggleGroup(group)}
                className="w-full px-3 py-2 flex items-center justify-between bg-muted/40 text-left hover:bg-muted/60 rounded-t-md"
              >
                <span className="font-medium text-sm">{group.group}</span>
                <Badge variant={allOn ? "default" : someOn ? "secondary" : "outline"} className="text-xs">
                  {groupKeys.filter((k) => selected.has(k)).length} / {groupKeys.length}
                </Badge>
              </button>
              <div className="p-3 space-y-2">
                {group.permissions.map((p) => (
                  <label
                    key={p.key}
                    className="flex items-start gap-2 cursor-pointer hover:bg-muted/30 rounded p-1 -m-1"
                  >
                    <Checkbox
                      checked={selected.has(p.key)}
                      onCheckedChange={() => toggle(p.key)}
                      className="mt-0.5"
                    />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm">{p.label}</p>
                      <p className="text-xs text-muted-foreground font-mono">{p.key}</p>
                    </div>
                  </label>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
