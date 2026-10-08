import { readFile, writeFile } from "node:fs/promises";
import { compile } from "json-schema-to-typescript";
const schema = JSON.parse(
  await readFile(
    new URL("../src/dashboard.schema.json", import.meta.url),
    "utf8",
  ),
);
await writeFile(
  new URL("../src/types.ts", import.meta.url),
  await compile(schema, "DashboardSnapshot", { additionalProperties: false }),
);
