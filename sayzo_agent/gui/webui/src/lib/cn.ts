// Tiny class-name combiner. Drops falsy values; joins the rest with spaces.
// Mirrors clsx's API surface for the bits we use, without the dep.
type Arg = string | number | false | null | undefined;
export function cn(...args: Arg[]): string {
  return args.filter(Boolean).join(" ");
}
