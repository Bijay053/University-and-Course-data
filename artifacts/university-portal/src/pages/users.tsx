import { useEffect, useMemo, useState, FormEvent } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Plus, Trash2, KeyRound, ShieldCheck, ShieldOff,
  Eye, EyeOff, Tag, Pencil, Users,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Switch } from "@/components/ui/switch";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog, DialogContent, DialogDescription,
  DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel,
  AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useToast } from "@/hooks/use-toast";
import { useAuth } from "@/context/auth";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

interface UserRow {
  id: number;
  email: string;
  full_name: string;
  is_active: boolean;
  is_super_admin: boolean;
  role_id: number | null;
  role_name: string | null;
}

interface RoleRow {
  id: number;
  name: string;
  description: string;
  permissions: string[];
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

  const usersQ = useQuery({ queryKey: ["users"], queryFn: () => api<UserRow[]>("/api/users") });
  const rolesQ = useQuery({ queryKey: ["roles"], queryFn: () => api<RoleRow[]>("/api/roles") });
  const registryQ = useQuery({
    queryKey: ["permissions-registry"],
    queryFn: () => api<RegistryGroup[]>("/api/permissions/registry"),
  });

  const [createUserOpen, setCreateUserOpen] = useState(false);
  const [editingUser, setEditingUser] = useState<UserRow | null>(null);
  const [permsFor, setPermsFor] = useState<UserRow | null>(null);
  const [confirmDeleteUser, setConfirmDeleteUser] = useState<UserRow | null>(null);

  const [createRoleOpen, setCreateRoleOpen] = useState(false);
  const [editingRole, setEditingRole] = useState<RoleRow | null>(null);
  const [confirmDeleteRole, setConfirmDeleteRole] = useState<RoleRow | null>(null);

  const refreshUsers = () => qc.invalidateQueries({ queryKey: ["users"] });
  const refreshRoles = () => {
    qc.invalidateQueries({ queryKey: ["roles"] });
    qc.invalidateQueries({ queryKey: ["users"] });
  };

  return (
    <div className="container mx-auto p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Users & Permissions</h1>
        <p className="text-sm text-muted-foreground">
          Manage team members, define reusable roles, and set per-user access.
        </p>
      </div>

      <Tabs defaultValue="users">
        <TabsList>
          <TabsTrigger value="users" className="gap-2">
            <Users className="h-4 w-4" /> Users
          </TabsTrigger>
          <TabsTrigger value="roles" className="gap-2">
            <Tag className="h-4 w-4" /> Roles
          </TabsTrigger>
        </TabsList>

        {/* ── USERS TAB ─────────────────────────────────────────────── */}
        <TabsContent value="users" className="mt-4">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between py-4">
              <CardTitle className="text-base">All users</CardTitle>
              <Button size="sm" onClick={() => setCreateUserOpen(true)}>
                <Plus className="h-4 w-4 mr-1" /> Add user
              </Button>
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
                            {u.is_active
                              ? <Badge className="bg-emerald-600">Active</Badge>
                              : <Badge variant="secondary">Disabled</Badge>}
                          </td>
                          <td className="px-4 py-3">
                            {u.is_super_admin ? (
                              <Badge className="bg-violet-600 gap-1">
                                <ShieldCheck className="h-3 w-3" /> Super admin
                              </Badge>
                            ) : u.role_name ? (
                              <Badge variant="outline" className="gap-1">
                                <Tag className="h-3 w-3" /> {u.role_name}
                              </Badge>
                            ) : (
                              <Badge variant="outline" className="text-muted-foreground">No role</Badge>
                            )}
                          </td>
                          <td className="px-4 py-3">
                            <div className="flex items-center justify-end gap-2">
                              <Button
                                size="sm" variant="outline"
                                onClick={() => setPermsFor(u)}
                                disabled={u.is_super_admin}
                                title={u.is_super_admin ? "Super admin has all permissions" : "Edit extra permissions"}
                              >
                                <KeyRound className="h-3.5 w-3.5 mr-1" /> Permissions
                              </Button>
                              <Button size="sm" variant="outline" onClick={() => setEditingUser(u)}>
                                <Pencil className="h-3.5 w-3.5" />
                              </Button>
                              <Button
                                size="sm" variant="outline"
                                className="text-destructive hover:bg-destructive/10"
                                onClick={() => setConfirmDeleteUser(u)}
                                disabled={u.id === me?.id}
                                title={u.id === me?.id ? "Cannot delete your own account" : "Delete user"}
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
        </TabsContent>

        {/* ── ROLES TAB ─────────────────────────────────────────────── */}
        <TabsContent value="roles" className="mt-4">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between py-4">
              <div>
                <CardTitle className="text-base">Roles</CardTitle>
                <p className="text-xs text-muted-foreground mt-0.5">
                  Roles are named permission presets. Assign a role to a user to give them its permissions in bulk.
                  Extra permissions can still be added per-user on top of the role.
                </p>
              </div>
              <Button size="sm" onClick={() => setCreateRoleOpen(true)}>
                <Plus className="h-4 w-4 mr-1" /> Add role
              </Button>
            </CardHeader>
            <CardContent className="p-0">
              {rolesQ.isLoading ? (
                <p className="p-6 text-sm text-muted-foreground">Loading…</p>
              ) : rolesQ.error ? (
                <p className="p-6 text-sm text-destructive">{(rolesQ.error as Error).message}</p>
              ) : (rolesQ.data ?? []).length === 0 ? (
                <p className="p-6 text-sm text-muted-foreground">No roles yet. Create one to get started.</p>
              ) : (
                <div className="divide-y">
                  {(rolesQ.data ?? []).map((r) => (
                    <div key={r.id} className="px-4 py-3 flex items-start justify-between gap-4 hover:bg-muted/20">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <p className="font-medium text-sm">{r.name}</p>
                          <Badge variant="outline" className="text-xs">
                            {r.permissions.length} permission{r.permissions.length !== 1 ? "s" : ""}
                          </Badge>
                        </div>
                        {r.description && (
                          <p className="text-xs text-muted-foreground mt-0.5">{r.description}</p>
                        )}
                        {r.permissions.length > 0 && (
                          <div className="flex flex-wrap gap-1 mt-1.5">
                            {r.permissions.slice(0, 8).map((k) => (
                              <span key={k} className="text-xs bg-muted rounded px-1.5 py-0.5 font-mono">{k}</span>
                            ))}
                            {r.permissions.length > 8 && (
                              <span className="text-xs text-muted-foreground">+{r.permissions.length - 8} more</span>
                            )}
                          </div>
                        )}
                      </div>
                      <div className="flex gap-2 shrink-0">
                        <Button size="sm" variant="outline" onClick={() => setEditingRole(r)}>
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          size="sm" variant="outline"
                          className="text-destructive hover:bg-destructive/10"
                          onClick={() => setConfirmDeleteRole(r)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {/* ── Dialogs ─────────────────────────────────────────────────── */}
      <CreateUserDialog
        open={createUserOpen}
        onOpenChange={setCreateUserOpen}
        registry={registryQ.data ?? []}
        roles={rolesQ.data ?? []}
        onCreated={() => { refreshUsers(); toast({ title: "User created" }); }}
      />
      <EditUserDialog
        user={editingUser}
        roles={rolesQ.data ?? []}
        onClose={() => setEditingUser(null)}
        onSaved={() => { refreshUsers(); toast({ title: "User updated" }); }}
        currentUserId={me?.id}
      />
      <PermissionsDialog
        user={permsFor}
        registry={registryQ.data ?? []}
        onClose={() => setPermsFor(null)}
        onSaved={() => toast({ title: "Permissions updated" })}
      />

      <CreateRoleDialog
        open={createRoleOpen}
        onOpenChange={setCreateRoleOpen}
        registry={registryQ.data ?? []}
        onCreated={() => { refreshRoles(); toast({ title: "Role created" }); }}
      />
      <EditRoleDialog
        role={editingRole}
        registry={registryQ.data ?? []}
        onClose={() => setEditingRole(null)}
        onSaved={() => { refreshRoles(); toast({ title: "Role updated" }); }}
      />

      {/* Delete user confirm */}
      <AlertDialog open={!!confirmDeleteUser} onOpenChange={(o) => !o && setConfirmDeleteUser(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete user?</AlertDialogTitle>
            <AlertDialogDescription>
              Permanently removes <strong>{confirmDeleteUser?.email}</strong> and all their permissions.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={async () => {
              if (!confirmDeleteUser) return;
              try {
                await api(`/api/users/${confirmDeleteUser.id}`, { method: "DELETE" });
                refreshUsers();
                toast({ title: "User deleted" });
              } catch (err) {
                toast({ title: "Delete failed", description: (err as Error).message, variant: "destructive" });
              } finally { setConfirmDeleteUser(null); }
            }}>Delete</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete role confirm */}
      <AlertDialog open={!!confirmDeleteRole} onOpenChange={(o) => !o && setConfirmDeleteRole(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete role?</AlertDialogTitle>
            <AlertDialogDescription>
              Permanently deletes <strong>{confirmDeleteRole?.name}</strong>. Users assigned to this role will lose its permissions.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={async () => {
              if (!confirmDeleteRole) return;
              try {
                await api(`/api/roles/${confirmDeleteRole.id}`, { method: "DELETE" });
                refreshRoles();
                toast({ title: "Role deleted" });
              } catch (err) {
                toast({ title: "Delete failed", description: (err as Error).message, variant: "destructive" });
              } finally { setConfirmDeleteRole(null); }
            }}>Delete</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

// ---------------------------------------------------------------------------
// User dialogs
// ---------------------------------------------------------------------------

function CreateUserDialog({ open, onOpenChange, registry, roles, onCreated }: {
  open: boolean; onOpenChange: (o: boolean) => void;
  registry: RegistryGroup[]; roles: RoleRow[]; onCreated: () => void;
}) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [isSuper, setIsSuper] = useState(false);
  const [roleId, setRoleId] = useState<number | "">("");
  const [perms, setPerms] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) { setEmail(""); setName(""); setPassword(""); setIsSuper(false); setRoleId(""); setPerms(new Set()); setError(null); setShowPw(false); }
  }, [open]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < 8) { setError("Password must be at least 8 characters."); return; }
    setLoading(true);
    try {
      await api("/api/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, full_name: name, password, is_super_admin: isSuper, role_id: roleId || null, permissions: Array.from(perms) }),
      });
      onCreated(); onOpenChange(false);
    } catch (err) { setError((err as Error).message); }
    finally { setLoading(false); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Add user</DialogTitle>
          <DialogDescription>Create a new team member account.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>Email</Label>
              <Input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>Full name</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label>Password (min 8 characters)</Label>
            <div className="relative">
              <Input type={showPw ? "text" : "password"} required minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} className="pr-10" />
              <button type="button" onClick={() => setShowPw((v) => !v)} tabIndex={-1} className="absolute inset-y-0 right-0 flex items-center px-3 text-muted-foreground hover:text-foreground">
                {showPw ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </div>
          <div className="flex items-center justify-between rounded-md border p-3 bg-muted/30">
            <div>
              <p className="text-sm font-medium">Super admin</p>
              <p className="text-xs text-muted-foreground">Bypasses all permission checks.</p>
            </div>
            <Switch checked={isSuper} onCheckedChange={setIsSuper} />
          </div>
          {!isSuper && (
            <>
              <div className="space-y-1.5">
                <Label>Role <span className="text-muted-foreground font-normal">(optional)</span></Label>
                <select
                  className="w-full border rounded-md px-3 py-2 text-sm bg-background"
                  value={roleId}
                  onChange={(e) => setRoleId(e.target.value ? Number(e.target.value) : "")}
                >
                  <option value="">— No role —</option>
                  {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
                </select>
                {roleId && (
                  <p className="text-xs text-muted-foreground">
                    This role grants: {roles.find(r => r.id === roleId)?.permissions.join(", ") || "no permissions"}
                  </p>
                )}
              </div>
              <div className="space-y-2">
                <Label>Extra permissions <span className="text-muted-foreground font-normal">(on top of role)</span></Label>
                <PermissionsGrid registry={registry} selected={perms} onChange={setPerms} />
              </div>
            </>
          )}
          {error && <p className="text-sm text-destructive bg-destructive/10 rounded-md px-3 py-2">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={loading}>{loading ? "Creating…" : "Create user"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function EditUserDialog({ user, roles, onClose, onSaved, currentUserId }: {
  user: UserRow | null; roles: RoleRow[];
  onClose: () => void; onSaved: () => void; currentUserId?: number;
}) {
  const [name, setName] = useState("");
  const [active, setActive] = useState(true);
  const [isSuper, setIsSuper] = useState(false);
  const [roleId, setRoleId] = useState<number | "">("");
  const [newPw, setNewPw] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (user) { setName(user.full_name); setActive(user.is_active); setIsSuper(user.is_super_admin); setRoleId(user.role_id ?? ""); setNewPw(""); setError(null); setShowPw(false); }
  }, [user]);

  if (!user) return null;
  const isSelf = user.id === currentUserId;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!user) return;
    if (newPw && newPw.length < 8) { setError("New password must be at least 8 characters."); return; }
    setLoading(true);
    try {
      const body: Record<string, unknown> = { full_name: name, is_active: active, is_super_admin: isSuper, role_id: roleId || null };
      if (newPw) body.new_password = newPw;
      await api(`/api/users/${user.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      onSaved(); onClose();
    } catch (err) { setError((err as Error).message); }
    finally { setLoading(false); }
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
            <Label>Full name</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} />
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
              <p className="text-xs text-muted-foreground">Bypasses all permission checks.</p>
            </div>
            <Switch checked={isSuper} onCheckedChange={setIsSuper} disabled={isSelf} />
          </div>
          {!isSuper && (
            <div className="space-y-1.5">
              <Label>Role</Label>
              <select
                className="w-full border rounded-md px-3 py-2 text-sm bg-background"
                value={roleId}
                onChange={(e) => setRoleId(e.target.value ? Number(e.target.value) : "")}
              >
                <option value="">— No role —</option>
                {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
              </select>
            </div>
          )}
          <div className="space-y-1.5">
            <Label>New password <span className="text-muted-foreground font-normal">(leave blank to keep)</span></Label>
            <div className="relative">
              <Input type={showPw ? "text" : "password"} value={newPw} onChange={(e) => setNewPw(e.target.value)} placeholder="Leave blank to keep current" className="pr-10" />
              <button type="button" onClick={() => setShowPw((v) => !v)} tabIndex={-1} className="absolute inset-y-0 right-0 flex items-center px-3 text-muted-foreground hover:text-foreground">
                {showPw ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </div>
          {error && <p className="text-sm text-destructive bg-destructive/10 rounded-md px-3 py-2">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={loading}>{loading ? "Saving…" : "Save"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function PermissionsDialog({ user, registry, onClose, onSaved }: {
  user: UserRow | null; registry: RegistryGroup[];
  onClose: () => void; onSaved: () => void;
}) {
  const [rolePerms, setRolePerms] = useState<string[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    setLoading(true);
    api<{ role_permissions: string[]; user_permissions: string[] }>(`/api/users/${user.id}/permissions`)
      .then((d) => { setRolePerms(d.role_permissions); setSelected(new Set(d.user_permissions)); })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [user]);

  if (!user) return null;

  async function save() {
    if (!user) return;
    setSaving(true); setError(null);
    try {
      await api(`/api/users/${user.id}/permissions`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ permissions: Array.from(selected) }),
      });
      onSaved(); onClose();
    } catch (err) { setError((err as Error).message); }
    finally { setSaving(false); }
  }

  return (
    <Dialog open={!!user} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Extra permissions</DialogTitle>
          <DialogDescription>
            Additional permissions for <span className="font-mono">{user.email}</span> on top of their role.
            {rolePerms.length > 0 && (
              <span className="block mt-1 text-emerald-700">
                Already granted via role: {rolePerms.join(", ")}
              </span>
            )}
          </DialogDescription>
        </DialogHeader>
        {loading ? (
          <p className="text-sm text-muted-foreground py-6">Loading…</p>
        ) : (
          <PermissionsGrid registry={registry} selected={selected} onChange={setSelected} roleGranted={new Set(rolePerms)} />
        )}
        {error && <p className="text-sm text-destructive bg-destructive/10 rounded-md px-3 py-2">{error}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={save} disabled={saving || loading}>{saving ? "Saving…" : "Save permissions"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Role dialogs
// ---------------------------------------------------------------------------

function CreateRoleDialog({ open, onOpenChange, registry, onCreated }: {
  open: boolean; onOpenChange: (o: boolean) => void;
  registry: RegistryGroup[]; onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [perms, setPerms] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => { if (!open) { setName(""); setDesc(""); setPerms(new Set()); setError(null); } }, [open]);

  async function submit(e: FormEvent) {
    e.preventDefault(); setError(null);
    if (!name.trim()) { setError("Role name is required."); return; }
    setLoading(true);
    try {
      await api("/api/roles", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim(), description: desc.trim(), permissions: Array.from(perms) }) });
      onCreated(); onOpenChange(false);
    } catch (err) { setError((err as Error).message); }
    finally { setLoading(false); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Create role</DialogTitle>
          <DialogDescription>Define a reusable set of permissions you can assign to users.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-1.5">
            <Label>Role name</Label>
            <Input placeholder="e.g. Editor, Viewer, Data Entry" required value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>Description <span className="text-muted-foreground font-normal">(optional)</span></Label>
            <Input placeholder="What this role is for" value={desc} onChange={(e) => setDesc(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label>Permissions included in this role</Label>
            <PermissionsGrid registry={registry} selected={perms} onChange={setPerms} />
          </div>
          {error && <p className="text-sm text-destructive bg-destructive/10 rounded-md px-3 py-2">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={loading}>{loading ? "Creating…" : "Create role"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function EditRoleDialog({ role, registry, onClose, onSaved }: {
  role: RoleRow | null; registry: RegistryGroup[];
  onClose: () => void; onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [perms, setPerms] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (role) { setName(role.name); setDesc(role.description); setPerms(new Set(role.permissions)); setError(null); }
  }, [role]);

  if (!role) return null;

  async function submit(e: FormEvent) {
    e.preventDefault(); setError(null);
    if (!name.trim()) { setError("Role name is required."); return; }
    setLoading(true);
    try {
      await api(`/api/roles/${role!.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim(), description: desc.trim(), permissions: Array.from(perms) }) });
      onSaved(); onClose();
    } catch (err) { setError((err as Error).message); }
    finally { setLoading(false); }
  }

  return (
    <Dialog open={!!role} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Edit role</DialogTitle>
          <DialogDescription>Update the name, description, and permissions for <strong>{role.name}</strong>.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-1.5">
            <Label>Role name</Label>
            <Input required value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>Description</Label>
            <Input value={desc} onChange={(e) => setDesc(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label>Permissions</Label>
            <PermissionsGrid registry={registry} selected={perms} onChange={setPerms} />
          </div>
          {error && <p className="text-sm text-destructive bg-destructive/10 rounded-md px-3 py-2">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={loading}>{loading ? "Saving…" : "Save role"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Shared PermissionsGrid
// ---------------------------------------------------------------------------

function PermissionsGrid({ registry, selected, onChange, roleGranted }: {
  registry: RegistryGroup[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
  roleGranted?: Set<string>;
}) {
  const allKeys = useMemo(() => registry.flatMap((g) => g.permissions.map((p) => p.key)), [registry]);
  const allChecked = allKeys.length > 0 && allKeys.every((k) => selected.has(k));
  const noneChecked = allKeys.every((k) => !selected.has(k));

  function toggle(key: string) { const n = new Set(selected); n.has(key) ? n.delete(key) : n.add(key); onChange(n); }
  function toggleGroup(group: RegistryGroup) {
    const n = new Set(selected);
    const gk = group.permissions.map((p) => p.key);
    gk.every((k) => n.has(k)) ? gk.forEach((k) => n.delete(k)) : gk.forEach((k) => n.add(k));
    onChange(n);
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-xs">
        <Button type="button" size="sm" variant="outline" onClick={() => onChange(new Set(allKeys))} disabled={allChecked}>Grant all</Button>
        <Button type="button" size="sm" variant="outline" onClick={() => onChange(new Set())} disabled={noneChecked}>Revoke all</Button>
        <span className="text-muted-foreground ml-auto">{selected.size} / {allKeys.length} granted</span>
      </div>
      <div className="space-y-3">
        {registry.map((group) => {
          const gk = group.permissions.map((p) => p.key);
          const allOn = gk.every((k) => selected.has(k));
          const someOn = gk.some((k) => selected.has(k));
          return (
            <div key={group.group} className="rounded-md border">
              <button type="button" onClick={() => toggleGroup(group)}
                className="w-full px-3 py-2 flex items-center justify-between bg-muted/40 text-left hover:bg-muted/60 rounded-t-md">
                <span className="font-medium text-sm">{group.group}</span>
                <Badge variant={allOn ? "default" : someOn ? "secondary" : "outline"} className="text-xs">
                  {gk.filter((k) => selected.has(k)).length} / {gk.length}
                </Badge>
              </button>
              <div className="p-3 space-y-2">
                {group.permissions.map((p) => {
                  const fromRole = roleGranted?.has(p.key);
                  return (
                    <label key={p.key} className="flex items-start gap-2 cursor-pointer hover:bg-muted/30 rounded p-1 -m-1">
                      <Checkbox checked={selected.has(p.key) || !!fromRole} onCheckedChange={() => !fromRole && toggle(p.key)} disabled={!!fromRole} className="mt-0.5" />
                      <div className="flex-1 min-w-0">
                        <p className="text-sm">{p.label} {fromRole && <span className="text-xs text-emerald-600 ml-1">(via role)</span>}</p>
                        <p className="text-xs text-muted-foreground font-mono">{p.key}</p>
                      </div>
                    </label>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
