// Test-only Worker: exposes worker/store.ts directly so the Python tests can check
// the real D1 code (run locally by `wrangler dev`, with a throwaway local database).
import { handleStore, type StoreEnv } from "../../worker/store";

export default {
	fetch: (request: Request, env: StoreEnv) => handleStore(request, env),
} satisfies ExportedHandler<StoreEnv>;
