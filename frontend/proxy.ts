import { NextResponse, type NextRequest } from "next/server";

// Lightweight guard: the real authorization happens in the API. The httpOnly refresh cookie is scoped to
// the API path, so we rely on the non-sensitive "btp_session" marker (or a live access token).
export function proxy(request: NextRequest) {
  const hasSession = request.cookies.has("btp_session") || request.cookies.has("access_token");
  if (!hasSession) {
    const url = request.nextUrl.clone();
    url.pathname = "/auth/login";
    url.searchParams.set("next", request.nextUrl.pathname);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/dashboard/:path*", "/admin/:path*"],
};
