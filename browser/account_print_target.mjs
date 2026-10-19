// URL allowlist checked before opening an official accounting print page.
export function validAccountPrintTarget(target, number) {
  return target.origin === "https://account.ncu.edu.tw" &&
    /^\/APSWIS_Q\/TMP_PAG_Q\/NCU\d{1,3}\.asp$/.test(target.pathname) &&
    target.searchParams.get("APYNO") === number &&
    ["14", "15"].includes(target.searchParams.get("APPYSET")) &&
    target.searchParams.size === 4 &&
    ["APPYYEAR", "APYNO", "APYADD", "APPYSET"].every(
      name => target.searchParams.has(name));
}
