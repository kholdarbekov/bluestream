/**
 * Every query that reads place-group state. A place-group mutation (create,
 * add, remove, rename, dismiss) changes what ALL of them show — the estate-wide list,
 * the suggestions, each customer's grouped addresses, each group's detail and
 * the customer map's place markers — so they refresh together from ONE list
 * rather than each surface remembering its own subset.
 */
export const PLACE_GROUP_QUERY_KEYS = [
  ['placeGroupsList'],
  ['placeGroupSuggestionsGlobal'],
  ['placeGroupAddresses'],
  ['placeGroup'],
  ['placeGroupSuggestions'],
  ['customerMapPins'],
];

export function invalidatePlaceGroupQueries(queryClient) {
  PLACE_GROUP_QUERY_KEYS.forEach((queryKey) => {
    queryClient.invalidateQueries({ queryKey });
  });
}
