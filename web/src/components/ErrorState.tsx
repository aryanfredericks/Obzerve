import { ApiError } from "../api";

export function ErrorState({ error }: { error: Error }) {
  const unauthorized = error instanceof ApiError && (error.status === 401 || error.status === 403);
  return (
    <div className="empty error-state">
      <p>
        <strong>{unauthorized ? "Not authorized" : "Could not load data"}</strong>
      </p>
      <p className="muted">{error.message}</p>
      {unauthorized && <p className="muted">Set a project API key with the “read” scope in the header.</p>}
      {error instanceof TypeError && <p className="muted">Is the Obzerve server running on port 8000?</p>}
    </div>
  );
}
