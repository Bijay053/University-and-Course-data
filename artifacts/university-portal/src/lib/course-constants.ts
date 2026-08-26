import taxonomy from "../../../../shared/course-taxonomy.json";

export const CATEGORIES: Record<string, string[]> = taxonomy.categories;

export const CATEGORY_NAMES = Object.keys(CATEGORIES).sort();

export const DEGREE_LEVELS = [
  "Associate Degree or Equivalent",
  "Associate Degree",
  "Certificate",
  "Diploma",
  "Certificate & Diploma",
  "Pathway to Undergraduate",
  "Bachelor",
  "Graduate Certificate",
  "Graduate Diploma",
  "Graduate Certificate & Diploma",
  "Master",
  "Bachelor Dual Degree",
  "Master Dual Degree",
  "Dual Degree",
  "PhD",
  "Doctor/Doctorate",
  "English Language",
];

export const STUDY_MODES = ["On Campus", "Online", "Blended", "Both"];

export const STUDY_LOADS = ["Full Time", "Part Time", "Both"];

export const INTAKE_MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

export const FEE_TERMS = ["Per Year", "Per Semester", "Per Subject", "Total"];

export const CURRENCIES = ["AUD", "USD", "GBP", "EUR", "NZD", "CAD", "SGD"];

export const ENGLISH_TEST_TYPES = ["IELTS", "PTE", "TOEFL", "Duolingo", "Cambridge", "Other"];

export function getSubCategories(category: string): string[] {
  return CATEGORIES[category] ?? [];
}
