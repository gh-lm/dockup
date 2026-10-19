/* The sign-in page's one piece of script: the round button that shows
   the password while it is pressed in, so a typo can be checked before
   signing in. Nothing else on this page needs script. */
const pwBox = document.getElementById("password");
const showPw = document.getElementById("showPw");
if (pwBox && showPw) {
  showPw.addEventListener("click", () => {
    const shown = pwBox.type === "text";
    pwBox.type = shown ? "password" : "text";
    showPw.setAttribute("aria-pressed", String(!shown));
    showPw.classList.toggle("active", !shown);
    showPw.dataset.tip = shown ? "Show password" : "Hide password";
    showPw.setAttribute("aria-label", shown ? "Show the password" : "Hide the password");
  });
}
