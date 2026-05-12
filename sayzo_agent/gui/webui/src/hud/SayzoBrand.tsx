import logoSvgUrl from "../assets/logo.svg";

interface Props {
  size?: number;
  textClassName?: string;
}

export function SayzoBrand({ size = 24, textClassName = "text-[15px]" }: Props) {
  return (
    <div className="flex items-center gap-2">
      <img
        src={logoSvgUrl}
        alt=""
        width={size}
        height={size}
        className="hud-logo-img shrink-0"
      />
      <span
        className={`${textClassName} font-semibold tracking-tight text-ink`}
      >
        Sayzo
      </span>
    </div>
  );
}
