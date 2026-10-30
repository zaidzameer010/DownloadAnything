import { useEffect, useState } from 'react'
import { ListBox, Select } from '@heroui/react'
import { getAppSettings, logError } from '../tauri'
import type { Category } from '../types'

/** Categories only matter when a dialog is open — refetch each time. */
export function useCategories(active: boolean) {
  const [cats, setCats] = useState<Category[]>([])
  useEffect(() => {
    if (!active) return
    getAppSettings()
      .then((s) => setCats(s.categories ?? []))
      .catch((e) => logError('categories load', e))
  }, [active])
  return cats
}

/** Category picker for the download dialogs — hidden when no categories
 *  exist so an unconfigured install adds no noise. */
export function CategorySelect({
  cats,
  value,
  onChange,
}: {
  cats: Category[]
  value: string
  onChange: (v: string) => void
}) {
  if (cats.length === 0) return null
  return (
    <Select
      aria-label="Output category"
      variant="secondary"
      placeholder="Downloads"
      value={value || null}
      onChange={(k) => onChange(String(k))}
      className="w-40"
    >
      <Select.Trigger>
        <Select.Value />
        <Select.Indicator />
      </Select.Trigger>
      <Select.Popover>
        <ListBox>
          {cats.map((c) => (
            <ListBox.Item key={c.name} id={c.name} textValue={c.name}>
              {c.name}
              <ListBox.ItemIndicator />
            </ListBox.Item>
          ))}
        </ListBox>
      </Select.Popover>
    </Select>
  )
}
