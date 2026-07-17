import babel from "@rolldown/plugin-babel";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import { readFileSync } from "fs";
import { defineConfig } from "vite";

const pkg = JSON.parse(readFileSync("./package.json", "utf-8"));

// https://vite.dev/config/
export default defineConfig({
	plugins: [react(), babel({ presets: [reactCompilerPreset()] })],
	define: {
		"import.meta.env.APP_VERSION": JSON.stringify(pkg.version),
	},
});
