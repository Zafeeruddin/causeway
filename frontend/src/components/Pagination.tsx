"use client";

import { Button } from "./ui";

/**
 * A pager for a server-paged list.
 *
 * It shows the first page, the last page and the neighbours of the current one,
 * so the control stays the same width whether there are three pages or three
 * hundred.
 */
export function Pagination({
  page,
  pages,
  setPage,
  label,
}: {
  page: number;
  pages: number;
  setPage: (page: number) => void;
  /** Named for the thing being paged, so a screen reader hears which list. */
  label: string;
}) {
  if (pages <= 1) return null;
  const visible = paginationWindow(page, pages);

  return (
    <nav className="flex flex-wrap items-center justify-center gap-1 pt-2" aria-label={label}>
      <Button size="sm" onClick={() => setPage(page - 1)} disabled={page === 1}>
        Previous
      </Button>
      {visible.map((item, index) =>
        item === "gap" ? (
          <span key={`gap-${index}`} className="px-1.5 text-xs text-fg-3" aria-hidden>
            …
          </span>
        ) : (
          <Button
            key={item}
            size="sm"
            variant={item === page ? "primary" : "quiet"}
            onClick={() => setPage(item)}
            aria-current={item === page ? "page" : undefined}
            aria-label={`Page ${item}`}
          >
            {item}
          </Button>
        ),
      )}
      <Button size="sm" onClick={() => setPage(page + 1)} disabled={page === pages}>
        Next
      </Button>
    </nav>
  );
}

function paginationWindow(page: number, pages: number): (number | "gap")[] {
  const wanted = new Set([1, pages, page - 1, page, page + 1]);
  const numbers = [...wanted].filter((value) => value >= 1 && value <= pages).sort((a, b) => a - b);
  const result: (number | "gap")[] = [];
  for (const value of numbers) {
    const previous = result[result.length - 1];
    if (typeof previous === "number" && value - previous > 1) result.push("gap");
    result.push(value);
  }
  return result;
}
