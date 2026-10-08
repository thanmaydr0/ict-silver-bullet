import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, readdirSync, existsSync } from "node:fs";
import { resolve, relative } from "node:path";
import { fileURLToPath } from "node:url";
const root = resolve(fileURLToPath(new URL("../", import.meta.url)));
const walk = (dir) =>
  readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(resolve(dir, e.name)) : [resolve(dir, e.name)],
  );
const inputs = readdirSync(root, { withFileTypes: true }).flatMap((e) =>
  e.isFile() && /\.(json|js|html)$/.test(e.name)
    ? [resolve(root, e.name)]
    : e.isDirectory() && ["src", "public", "scripts"].includes(e.name)
      ? walk(resolve(root, e.name))
      : [],
);
const source = createHash("sha256");
for (const file of inputs.sort())
  source
    .update(relative(root, file).replaceAll("\\", "/"))
    .update("\0")
    .update(readFileSync(file))
    .update("\0");
const sourceHash = source.digest("hex");
if (process.argv.includes("--source-hash")) {
  process.stdout.write(sourceHash);
} else {
  const dir = process.env.ICT_BUILD_DIR || "dist";
  if (!["dist", "dist.next"].includes(dir))
    throw new Error("Invalid build directory");
  const index = readFileSync(resolve(root, dir, "index.html"));
  if (!existsSync(resolve(root, dir, "assets")))
    throw new Error("Build assets missing");
  writeFileSync(
    resolve(root, dir, "build.json"),
    JSON.stringify(
      {
        schema_version: 1,
        build_id: createHash("sha256").update(index).digest("hex").slice(0, 16),
        source_hash: sourceHash,
        built_at: new Date().toISOString(),
      },
      null,
      2,
    ) + "\n",
  );
}
