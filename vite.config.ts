import tailwindcss from "@tailwindcss/vite";
import {defineConfig} from "vite";
import react, {reactCompilerPreset} from "@vitejs/plugin-react";
import babel from "@rolldown/plugin-babel";

export default defineConfig({
	plugins: [
		tailwindcss(),
		react(),
		babel({presets: [reactCompilerPreset()]}),
	],
	build: {
		rolldownOptions: {
			output: {
				codeSplitting: {
					groups: [
						{
							name: "react-vendor",
							priority: 20,
							test: /node_modules[\\/](?:react|react-dom|scheduler)[\\/]/,
						},
						{
							name: "ui-vendor",
							priority: 15,
							test: /node_modules[\\/](?:@heroui|react-aria|react-aria-components|react-stately|tailwind-variants)[\\/]/,
						},
						{
							name: "vendor",
							priority: 10,
							test: /node_modules/,
						},
					],
				},
			},
		},
	},
});
