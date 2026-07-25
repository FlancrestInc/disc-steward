# Recent Errors Timestamps and Clear Action

## Goal

Improve the main dashboard's recent-errors panel by showing when each error
was recorded and letting the user clear the current list without deleting
audit history.

## Design

The existing audit events already store `created_at` and `dismissed`. Reuse
those fields. The dashboard will render the timestamp for each visible error
event and add a POST form beside the section heading. The form will submit to
`/clear-errors`.

The new action will mark all currently visible error and failed audit events
as dismissed. It will not delete rows or affect non-error events. The request
will redirect back to `/`; the next dashboard summary will omit dismissed
events. If no errors remain, the panel will not render.

## Error handling

Use the existing request handler and database transaction patterns. The
action must be safe when there are no matching events and must preserve all
audit records.

## Testing

Add focused tests for:

- timestamp and clear-button rendering on the dashboard;
- clearing error events while preserving audit rows;
- the POST action redirect and empty dashboard state after clearing.

