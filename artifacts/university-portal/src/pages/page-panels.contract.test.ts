import { readdirSync, readFileSync } from "node:fs";
import { basename, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const pagesDirectory = resolve("src/pages");
const panelFilePattern = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*-panel\.tsx$/;

function discoverPanelFiles(fileNames: string[]) {
  return fileNames.filter((fileName) => panelFilePattern.test(fileName));
}

function pageLocalImportPath(importSource: string, pageName: string) {
  const relativePrefix = `./${pageName}`;
  const aliasPrefix = `@/pages/${pageName}`;

  for (const prefix of [relativePrefix, aliasPrefix]) {
    if (importSource === prefix) return "";
    if (importSource.startsWith(`${prefix}/`)) {
      return importSource.slice(prefix.length + 1);
    }
  }

  return undefined;
}

function rejectPageLocalPanelBarrels(source: string, pageName: string) {
  const namedBarrelImportPattern =
    /import\s*\{([^}]+)\}\s*from\s*["']([^"']+)["'];?/g;
  const defaultBarrelImportPattern =
    /import\s+([A-Za-z_$][\w$]*)\s+from\s*["']([^"']+)["'];?/g;

  for (const match of source.matchAll(namedBarrelImportPattern)) {
    const [, bindingsSource, importSource] = match;
    const importPath = pageLocalImportPath(importSource, pageName);
    if (importPath !== "" && importPath !== "index") continue;

    const panelBinding = bindingsSource
      .split(",")
      .map((binding) => binding.trim().split(/\s+as\s+/))
      .find((bindings) => bindings.some((binding) => binding.endsWith("Panel")));

    if (panelBinding) {
      throw new Error(
        `${panelBinding.at(-1)} is imported through the ${pageName} barrel; import panels from their source files`,
      );
    }
  }

  for (const match of source.matchAll(defaultBarrelImportPattern)) {
    const [, binding, importSource] = match;
    const importPath = pageLocalImportPath(importSource, pageName);
    if ((importPath === "" || importPath === "index") && binding.endsWith("Panel")) {
      throw new Error(
        `${binding} is imported through the ${pageName} barrel; import panels from their source files`,
      );
    }
  }
}

function discoverExtractedPanelImports(source: string, pageName: string) {
  rejectPageLocalPanelBarrels(source, pageName);

  const namedPanelImportPattern =
    /import\s*\{([^}]+)\}\s*from\s*["']([^"']+)["'];?/g;
  const defaultPanelImportPattern =
    /import\s+([A-Za-z_$][\w$]*)\s+from\s*["']([^"']+)["'];?/g;

  const namedPanels = [...source.matchAll(namedPanelImportPattern)].flatMap((match) => {
    const [, bindingsSource, importSource] = match;
    const importPath = pageLocalImportPath(importSource, pageName);
    if (!importPath || importPath === "index") return [];

    return bindingsSource
      .split(",")
      .map((binding) => binding.trim().split(/\s+as\s+/))
      .filter(([importedBinding]) => importedBinding.endsWith("Panel"))
      .map((bindings) => ({
        binding: bindings.at(-1) as string,
        fileName: `${importPath}.tsx`,
      }));
  });

  const defaultPanels = [...source.matchAll(defaultPanelImportPattern)].flatMap((match) => {
    const [, binding, importSource] = match;
    const importPath = pageLocalImportPath(importSource, pageName);
    if (!importPath || importPath === "index" || !binding.endsWith("Panel")) return [];
    return [{ binding, fileName: `${importPath}.tsx` }];
  });

  return [...namedPanels, ...defaultPanels];
}

function discoverAllExtractedPagePanels() {
  const pageFiles = readdirSync(pagesDirectory).filter((fileName) => fileName.endsWith(".tsx"));
  return pageFiles.flatMap((pageFile) => {
    const pageName = basename(pageFile, ".tsx");
    const pageSource = readFileSync(resolve(pagesDirectory, pageFile), "utf8");
    return discoverExtractedPanelImports(pageSource, pageName).map((panel) => ({
      ...panel,
      pageName,
    }));
  });
}

function discoverPanelPropsInterface(source: string, fileName: string) {
  const interfaceNames = [
    ...source.matchAll(/\binterface\s+([A-Za-z_$][\w$]*PanelProps)\b/g),
  ].map((match) => match[1]);

  expect(interfaceNames, `${fileName} must declare exactly one *PanelProps interface`).toHaveLength(1);
  return interfaceNames[0];
}

function readInterfaceDeclaration(source: string, interfaceName: string) {
  const declarationStart = source.indexOf(`interface ${interfaceName}`);
  expect(declarationStart, `${interfaceName} must remain an interface`).toBeGreaterThanOrEqual(0);

  const bodyStart = source.indexOf("{", declarationStart);
  expect(bodyStart, `${interfaceName} must have a body`).toBeGreaterThanOrEqual(0);

  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(declarationStart, index + 1);
  }

  throw new Error(`${interfaceName} has an unclosed body`);
}

function expectPanelContractSafe(source: string, fileName: string) {
  const interfaceName = discoverPanelPropsInterface(source, fileName);
  const contract = readInterfaceDeclaration(source, interfaceName);

  expect(contract, `${fileName} must not use any in its prop contract`).not.toMatch(/\bany\b/);
  expect(contract, `${fileName} must not use a broad Record prop bag`).not.toMatch(
    /\bRecord\s*<\s*(?:string|number|symbol|PropertyKey)\s*,\s*(?:unknown|object)\s*>/,
  );
  expect(contract, `${fileName} must not use a broad index-signature prop bag`).not.toMatch(
    /\[\s*[\w$]+\s*:\s*(?:string|number|symbol)\s*\]\s*:\s*(?:any|unknown|object)\b/,
  );
}

describe("page panel contracts", () => {
  it("discovers panel contracts by filename without including helper files", () => {
    expect(
      discoverPanelFiles([
        "academic-panel.tsx",
        "course-requirements-panel.tsx",
        "course-formatters.tsx",
        "panel-helper.ts",
      ]),
    ).toEqual(["academic-panel.tsx", "course-requirements-panel.tsx"]);
  });

  it("requires extracted page panels to use the *-panel.tsx convention", () => {
    const sampleImports = `
      import { AcademicPanel } from "./university-detail/academic-panel";
      import { EnglishPanel as EnglishRequirements } from "./university-detail/english-section";
      import FeesPanel from "./university-detail/fees-section";
      import { OutcomesPanel } from "@/pages/university-detail/outcomes-panel";
      import ScholarshipsPanel from "@/pages/university-detail/scholarships-section";
      import PageHeader from "./university-detail/page-header";
      import { formatCourse } from "./university-detail/course-formatters";
      import { EmptyState } from "./university-detail/empty-state";
    `;

    expect(discoverExtractedPanelImports(sampleImports, "university-detail")).toEqual([
      { binding: "AcademicPanel", fileName: "academic-panel.tsx" },
      { binding: "EnglishRequirements", fileName: "english-section.tsx" },
      { binding: "OutcomesPanel", fileName: "outcomes-panel.tsx" },
      { binding: "FeesPanel", fileName: "fees-section.tsx" },
      { binding: "ScholarshipsPanel", fileName: "scholarships-section.tsx" },
    ]);

    const extractedPanels = discoverAllExtractedPagePanels();

    expect(extractedPanels.length, "at least one extracted page panel import must be discovered")
      .toBeGreaterThan(0);
    for (const { binding, fileName, pageName } of extractedPanels) {
      expect(
        panelFilePattern.test(fileName),
        `${binding} is an extracted ${pageName} panel, so ${fileName} must use the *-panel.tsx naming convention`,
      ).toBe(true);
    }
  });

  it("rejects page-local panel barrels without rejecting ordinary exports", () => {
    expect(() =>
      discoverExtractedPanelImports(
        `
          import { AcademicPanel } from "./university-detail";
          import { formatCourse } from "./university-detail/index";
        `,
        "university-detail",
      ),
    ).toThrow(/AcademicPanel is imported through the university-detail barrel/);

    expect(() =>
      discoverExtractedPanelImports(
        `import { EnglishPanel as EnglishRequirements } from "./university-detail/index";`,
        "university-detail",
      ),
    ).toThrow(/EnglishRequirements is imported through the university-detail barrel/);

    expect(() =>
      discoverExtractedPanelImports(
        `import FeesPanel from "./university-detail/index";`,
        "university-detail",
      ),
    ).toThrow(/FeesPanel is imported through the university-detail barrel/);

    expect(() =>
      discoverExtractedPanelImports(
        `import { OutcomesPanel as GraduateOutcomesPanel } from "@/pages/university-detail";`,
        "university-detail",
      ),
    ).toThrow(/GraduateOutcomesPanel is imported through the university-detail barrel/);

    expect(() =>
      discoverExtractedPanelImports(
        `import ScholarshipsPanel from "@/pages/university-detail/index";`,
        "university-detail",
      ),
    ).toThrow(/ScholarshipsPanel is imported through the university-detail barrel/);

    expect(
      discoverExtractedPanelImports(
        `
          import { formatCourse, EmptyState } from "./university-detail";
          import PageHeader from "./university-detail/index";
          import { formatOutcome } from "@/pages/university-detail";
          import AliasPageHeader from "@/pages/university-detail/index";
        `,
        "university-detail",
      ),
    ).toEqual([]);
  });

  it("keeps all extracted panel prop contracts closed and type-safe", () => {
    const extractedPanels = discoverAllExtractedPagePanels();

    expect(extractedPanels.length, "at least one extracted page panel must be discovered").toBeGreaterThan(0);

    for (const { fileName, pageName } of extractedPanels) {
      const source = readFileSync(resolve(pagesDirectory, pageName, fileName), "utf8");
      expectPanelContractSafe(source, `${pageName}/${fileName}`);
    }
  });

  it("rejects an unsafe extracted panel contract outside University Detail", () => {
    const unsafeCoursesPanel = `
      interface ResultsPanelProps {
        filters: Record<string, unknown>;
      }
    `;

    expect(() => expectPanelContractSafe(unsafeCoursesPanel, "courses/results-panel.tsx"))
      .toThrow(/must not use a broad Record prop bag/);
  });
});