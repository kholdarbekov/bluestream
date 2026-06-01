/**
 * Shared client-side pagination helper.
 *
 * The backend caps per_page at MAX_PAGE_SIZE (100), so any single large request
 * is silently truncated. Use this to fetch ALL rows by looping pages until the
 * server reports no next page (or returns a short/empty page).
 *
 * Works regardless of where the pagination meta lives in the response
 * (top-level `meta` for unwrapped bodies, or `data.meta` for raw axios
 * responses) and falls back to a full-page heuristic when no meta is present.
 *
 * @param {(page:number)=>Promise<any>} fetchPage - fetches one page (1-based)
 * @param {(response:any)=>any[]} extractItems - pulls the rows array from a response
 * @param {number} pageSize - per-request page size (defaults to MAX_PAGE_SIZE)
 * @returns {Promise<any[]>} all rows concatenated across pages
 */
export async function fetchAllPages(fetchPage, extractItems, pageSize = 100) {
  const all = [];
  const MAX_PAGES = 1000; // safety backstop against an unbounded loop
  let page = 1;
  while (page <= MAX_PAGES) {
    const response = await fetchPage(page);
    const items = extractItems(response) || [];
    all.push(...items);
    const meta = response?.meta || response?.data?.meta;
    const hasNext = meta ? meta.has_next : items.length === pageSize;
    if (!hasNext || items.length === 0) break;
    page += 1;
  }
  return all;
}
