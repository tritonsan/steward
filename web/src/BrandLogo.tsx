export function BrandLogo({
  size = 48,
  decorative = false,
}: {
  size?: number;
  decorative?: boolean;
}) {
  return (
    <img
      className="steward-logo"
      src="/steward-logo.png"
      width={size}
      height={size}
      alt={decorative ? "" : "Steward"}
      draggable={false}
    />
  );
}
