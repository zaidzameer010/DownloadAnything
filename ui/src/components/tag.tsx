import { Chip } from '@heroui/react'

/** Format-row chip — the soft small chip every metadata flag reuses. */
export function Tag({
  color,
  className,
  title,
  children,
}: {
  color?: React.ComponentProps<typeof Chip>['color']
  className?: string
  title?: string
  children: React.ReactNode
}) {
  return (
    <Chip
      size="sm"
      variant="soft"
      color={color}
      className={`${color ? '' : 'chip--quiet'} h-5 ${className ?? ''}`}
      title={title}
    >
      {children}
    </Chip>
  )
}
