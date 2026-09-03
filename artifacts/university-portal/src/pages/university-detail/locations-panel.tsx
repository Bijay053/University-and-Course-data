import React from "react";

type LocationsPanelProps = Record<string, any> & {
  locations: any[];
};

export function LocationsPanel(props: LocationsPanelProps) {
  const { Button, Check, Globe, Loader2, MapPin, Pencil, RefreshCw, Trash2, deleteLocation, geocodeLocation, locations, locsGeocodingId, locsLoading, locsSyncing, setLocsEditForm, setLocsEditId, syncLocations } = props;
  return (
        <div className="space-y-4">
          <div className="flex items-center justify-between rounded-xl border bg-card px-5 py-4 shadow-sm">
            <div>
              <h2 className="text-base font-semibold">Campus Locations</h2>
              <p className="text-xs text-muted-foreground mt-0.5">
                Extracted from course location fields. Use "Sync" to rebuild from current courses, then "Geocode" to add coordinates.
              </p>
            </div>
            <Button size="sm" variant="outline" onClick={syncLocations} disabled={locsSyncing} className="gap-1.5">
              {locsSyncing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
              {locsSyncing ? "Syncing…" : "Sync from Courses"}
            </Button>
          </div>

          {locsLoading && (
            <div className="py-12 text-center text-muted-foreground text-sm">Loading locations…</div>
          )}

          {!locsLoading && locations.length === 0 && (
            <div className="py-12 text-center border rounded-xl bg-muted/20">
              <MapPin className="w-10 h-10 mx-auto mb-3 text-muted-foreground/30" />
              <p className="font-medium text-gray-700">No locations yet</p>
              <p className="text-sm text-muted-foreground mt-1">
                Click <strong>Sync from Courses</strong> to extract campus locations from course data.
              </p>
            </div>
          )}

          {!locsLoading && locations.length > 0 && (
            <div className="border rounded-xl overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b text-xs font-semibold text-gray-500 uppercase tracking-wide">
                  <tr>
                    <th className="px-4 py-2.5 text-left">Campus Name</th>
                    <th className="px-4 py-2.5 text-left">City / State</th>
                    <th className="px-4 py-2.5 text-left">Country</th>
                    <th className="px-4 py-2.5 text-left">Coordinates</th>
                    <th className="px-4 py-2.5 text-center">Courses</th>
                    <th className="px-4 py-2.5 text-center">Verified</th>
                    <th className="px-4 py-2.5 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {locations.map((loc) => (
                    <tr key={loc.id} className="bg-white hover:bg-gray-50 transition-colors">
                      <td className="px-4 py-2.5 font-medium text-gray-800">{loc.displayName}</td>
                      <td className="px-4 py-2.5 text-gray-600">
                        {[loc.city, loc.stateRegion].filter(Boolean).join(", ") || <span className="text-muted-foreground">—</span>}
                      </td>
                      <td className="px-4 py-2.5 text-gray-600">{loc.country ?? <span className="text-muted-foreground">—</span>}</td>
                      <td className="px-4 py-2.5 text-xs text-gray-500 font-mono">
                        {loc.latitude != null && loc.longitude != null
                          ? `${loc.latitude.toFixed(4)}, ${loc.longitude.toFixed(4)}`
                          : <span className="text-muted-foreground">—</span>}
                      </td>
                      <td className="px-4 py-2.5 text-center">
                        <span className="inline-flex items-center justify-center w-6 h-6 rounded-full bg-blue-50 text-blue-700 text-xs font-semibold">{loc.courseCount}</span>
                      </td>
                      <td className="px-4 py-2.5 text-center">
                        {loc.isVerified
                          ? <Check className="w-4 h-4 text-green-600 mx-auto" />
                          : <span className="text-muted-foreground text-xs">—</span>}
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            size="sm" variant="ghost"
                            className="h-7 px-2 text-xs gap-1"
                            disabled={locsGeocodingId === loc.id}
                            onClick={() => geocodeLocation(loc.id)}
                            title="Geocode via OpenStreetMap"
                          >
                            {locsGeocodingId === loc.id ? <Loader2 className="w-3 h-3 animate-spin" /> : <Globe className="w-3 h-3" />}
                            Geocode
                          </Button>
                          <Button
                            size="sm" variant="ghost"
                            className="h-7 px-2 text-xs gap-1"
                            onClick={() => { setLocsEditId(loc.id); setLocsEditForm({ displayName: loc.displayName, city: loc.city ?? "", stateRegion: loc.stateRegion ?? "", country: loc.country ?? "", fullAddress: loc.fullAddress ?? "", latitude: loc.latitude ?? undefined, longitude: loc.longitude ?? undefined, isVerified: loc.isVerified }); }}
                          >
                            <Pencil className="w-3 h-3" /> Edit
                          </Button>
                          <Button
                            size="sm" variant="ghost"
                            className="h-7 px-2 text-xs text-red-500 hover:text-red-700 hover:bg-red-50 gap-1"
                            onClick={() => deleteLocation(loc.id)}
                          >
                            <Trash2 className="w-3 h-3" /> Delete
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      );
}
