import { useMemo, useState } from "react";
import { Check, ChevronsUpDown, Search } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

type SearchableSelectProps = {
  value: string;
  options: string[];
  onChange: (value: string) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  anyLabel?: string;
  className?: string;
};

export function SearchableSelect({
  value,
  options,
  onChange,
  placeholder = "Any",
  searchPlaceholder = "Search…",
  anyLabel = "— Any —",
  className,
}: SearchableSelectProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");

  const filteredOptions = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    if (!query) return options;
    return options.filter((option) =>
      option.toLocaleLowerCase().includes(query),
    );
  }, [options, search]);

  const select = (nextValue: string) => {
    onChange(nextValue);
    setOpen(false);
    setSearch("");
  };

  return (
    <Popover
      open={open}
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
        if (!nextOpen) setSearch("");
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          role="combobox"
          aria-expanded={open}
          aria-label={placeholder}
          className={cn(
            "flex h-9 w-full items-center justify-between rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm ring-offset-background focus:outline-none focus:ring-1 focus:ring-ring",
            className,
          )}
        >
          <span className={cn("truncate", !value && "text-muted-foreground")}>
            {value || placeholder}
          </span>
          <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        aria-label={`${placeholder} menu`}
        aria-description={`Search and select ${placeholder.toLocaleLowerCase()}`}
        align="start"
        side="bottom"
        sideOffset={4}
        avoidCollisions={false}
        className="z-50 w-[var(--radix-popover-trigger-width)] min-w-56 p-0"
      >
        <div className="flex h-9 items-center border-b px-3">
          <Search className="mr-2 h-4 w-4 shrink-0 text-muted-foreground" />
          <input
            autoFocus
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={searchPlaceholder}
            aria-label={searchPlaceholder}
            className="h-full min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
        </div>
        <div
          role="listbox"
          aria-label={`${placeholder} options`}
          className="max-h-40 overflow-y-auto p-1"
        >
          {!search && (
            <button
              type="button"
              role="option"
              aria-selected={!value}
              onClick={() => select("")}
              className="flex w-full items-center rounded-sm px-2 py-1.5 text-left text-sm outline-none hover:bg-accent focus:bg-accent"
            >
              <Check
                className={cn(
                  "mr-2 h-4 w-4 shrink-0",
                  value ? "opacity-0" : "opacity-100",
                )}
              />
              {anyLabel}
            </button>
          )}
          {filteredOptions.map((option) => (
            <button
              key={option}
              type="button"
              role="option"
              aria-selected={value === option}
              onClick={() => select(option)}
              className="flex w-full items-center rounded-sm px-2 py-1.5 text-left text-sm outline-none hover:bg-accent focus:bg-accent"
            >
              <Check
                className={cn(
                  "mr-2 h-4 w-4 shrink-0",
                  value === option ? "opacity-100" : "opacity-0",
                )}
              />
              <span className="truncate">{option}</span>
            </button>
          ))}
          {filteredOptions.length === 0 && (
            <p className="px-2 py-5 text-center text-sm text-muted-foreground">
              No matching qualifications
            </p>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}