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

export function filterUniversities<T extends FilterableUniversity>(
  universities: T[],
  filters: UniversityListFilter,
): T[] {
  return universities.filter((university) => {
    const status = university.certificationStatus ?? "draft";
    return (
      (filters.country === "all" || university.country === filters.country)
      && (filters.status === "all" || status === filters.status)
      && (
        filters.featured === "all"
        || (filters.featured === "featured" ? !!university.featured : !university.featured)
      )
    );
  });
}