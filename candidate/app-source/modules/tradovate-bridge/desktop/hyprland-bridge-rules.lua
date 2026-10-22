-- Decrypt OS Tradovate bridge windows (Hyprland 0.56 Lua rules). The desktop config includes this file.
-- Workspace 3: the Bridge window and login A's Tradovate window. Workspace 5: Bridge Advanced.
-- Extra logins get their own workspace from 6 up, so a sign-in window never covers the trading screens.
o.window({class = "chrome-127.0.0.1__bridge-Default"}, {workspace = "3 silent", tile = true, no_initial_focus = true})
o.window({class = "tvbridge-login-a"}, {workspace = "3 silent", tile = true, no_initial_focus = true})
o.window({class = "chrome-127.0.0.1__advanced-Default"}, {workspace = "5", tile = true})
o.window({class = "tvbridge-login-b"}, {workspace = "6 silent", tile = true, no_initial_focus = true})
o.window({class = "tvbridge-login-c"}, {workspace = "7 silent", tile = true, no_initial_focus = true})
o.window({class = "tvbridge-login-d"}, {workspace = "8 silent", tile = true, no_initial_focus = true})
o.window({class = "tvbridge-login-e"}, {workspace = "9 silent", tile = true, no_initial_focus = true})
