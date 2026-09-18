export type UniversityListFilter = {
  country: string;
  status: string;
  featured: string;
};

type FilterableUniversity = {
  country?: string | null;
  certificationStatus?: string | null;
  featured?: boolean | null;
};

const COUNTRY_ALIASES: Record<string, string> = {
  au: "Australia",
  australia: "Australia",
  australian: "Australia",
  uk: "United Kingdom",
  "united kingdom": "United Kingdom",
  usa: "United States",
  "united states": "United States",
};

export function normalizeUniversityCountry(country?: string | null): string {
  const trimmed = country?.trim() ?? "";
  if (!trimmed) return "";
  return COUNTRY_ALIASES[trimmed.toLocaleLowerCase()] ?? trimmed;
}

export function filterUniversities<T extends FilterableUniversity>(
  universities: T[],
  filters: UniversityListFilter,
): T[] {
  return universities.filter((university) => {
    const status = university.certificationStatus ?? "draft";
    return (
      (
        filters.country === "all"
        || normalizeUniversityCountry(university.country)
          === normalizeUniversityCountry(filters.country)
      )
      && (filters.status === "all" || status === filters.status)
      && (
        filters.featured === "all"
        || (filters.featured === "featured" ? !!university.featured : !university.featured)
      )
    );
  });
}