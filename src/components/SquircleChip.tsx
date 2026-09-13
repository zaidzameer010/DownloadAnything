import { Chip } from "@heroui/react";
import { Squircle } from "@squircle-js/react";
import type { ComponentProps } from "react";

export default function SquircleChip(props: ComponentProps<typeof Chip>) {
	return (
		<Squircle asChild cornerRadius={16}>
			<Chip {...props} />
		</Squircle>
	);
}
