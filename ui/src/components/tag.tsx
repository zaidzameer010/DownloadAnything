import { Chip } from '@heroui/react'

/** Quiet categorical metadata — format flags, durations, extractor names. */
export function Tag({
  className,
  title,
  children,
}: {
  className?: string
  title?: string
  children: React.ReactNode
}) {
  return (
    <Chip
      size="sm"
      variant="soft"
      className={`chip--quiet h-4.5 shrink-0 gap-1 px-1.5 ${className ?? ''}`}
      title={title}
    >
      {children}
    </Chip>
  )
}
