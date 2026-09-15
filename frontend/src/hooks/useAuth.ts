import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"

import {
  type Body_login_login_access_token as AccessToken,
  LoginService,
  type UserPublic,
  type UserRegister,
  UsersService,
} from "@/client"
import { handleError } from "@/utils"
import useCustomToast from "./useCustomToast"

const isLoggedIn = () => {
  return localStorage.getItem("access_token") !== null
}

const isRejectedSession = (error: unknown) => {
  const status = (error as { response?: { status?: number } })?.response?.status
  if (typeof status !== "number") return false
  // `GET /users/me` answers 401 without a token, 403 for one it cannot
  // validate, 404 when the token names a user that no longer exists (a
  // recreated database) and 400 for a deactivated user: every one of those
  // means this browser has no usable session. 429 is the one 4xx that is about
  // pacing rather than identity, and a 5xx or a network failure means the
  // server could not answer at all — neither is a reason to drop the token.
  return status >= 400 && status < 500 && status !== 429
}

/**
 * Drop a token the API no longer accepts, reporting whether it did.
 *
 * The shell only checks that a token is *present*, so a rejected one (expired,
 * or issued before the user was recreated) would otherwise leave the app
 * looking signed in: the sidebar silently loses the superuser entries, and the
 * pages that check the session themselves fail their own ``beforeLoad`` and
 * render "Oops!" instead of asking for a fresh login. The token must also be
 * gone before redirecting, because ``/login`` bounces a caller that still has
 * one straight back to the dashboard.
 */
const clearRejectedToken = (error: unknown) => {
  if (!isRejectedSession(error)) return false
  localStorage.removeItem("access_token")
  return true
}

const useAuth = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()

  const { data: user } = useQuery<UserPublic | null, Error>({
    queryKey: ["currentUser"],
    queryFn: async () => (await UsersService.readUserMe()).data,
    enabled: isLoggedIn(),
  })

  const signUpMutation = useMutation({
    mutationFn: (data: UserRegister) =>
      UsersService.registerUser({ body: data }),
    onSuccess: () => {
      navigate({ to: "/login" })
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["users"] })
    },
  })

  const login = async (data: AccessToken) => {
    const response = await LoginService.loginAccessToken({
      body: data,
    })
    localStorage.setItem("access_token", response.data.access_token)
  }

  const loginMutation = useMutation({
    mutationFn: login,
    onSuccess: () => {
      navigate({ to: "/" })
    },
    onError: handleError.bind(showErrorToast),
  })

  const logout = () => {
    localStorage.removeItem("access_token")
    navigate({ to: "/login" })
  }

  return {
    signUpMutation,
    loginMutation,
    logout,
    user,
  }
}

export { clearRejectedToken, isLoggedIn }
export default useAuth
