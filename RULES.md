# Rules for the modpack maintainer(s)

1. An update every two weeks, adding suggestions from the suggestions page. (https://kubabin.dev/suggest)
2. The deadline doesn't have to be very strictly met, it can range from the given Thursday to Sunday. During the school year, delays are acceptable.
3. Meeting the deadline means updating using the given procedures.
4. The maintainer has to post a changelog, including:
    - version number, using our versioning
    - mods that were added
    - rejected, with a precise and clear reason.
    No need for showing which mods were updated.
5. Versioning
    We use a X.Y.Z version format, where:
    - X means the season number (currently 3)
    - Y means the major release (increased during biweekly updates)
    - Z means the minor release (increased during hotfixes, patches, etc)
6. If there is a controversial decision made by the Modpack Maintainer, the community can ask for a poll. 
Then if majority of the players and admins disagree with Modpack Maintainer's decision, it will be reverted/changed.
7. Update procedures
    - test the modpack in singleplayer mode and local dedicated server
    - if the modpack doesn't work in singleplayer mode, fix it before updating.
    - announce downtime on #annoucments
    - stop the server, with a 1minute notice
    - once the server has stopped, **create a backup**. (important)
    - update the modpack on the server (using the updater script if it exists)
    - start the server to test it
    - if damages caused by an update are too big to continue, revert the update.
    - if the update works properly, publish it to GitHub and then Modrinth and CurseForge
    - Publish the changelog, as per said specification.
8. be civil
9. Be cooperative, respect other admin's decisions and talk instead of 'shut up'
10. You have the right to admit you're wrong, and please do that instead of arguing or being mean. We're all humans and we all make mistakes.
11. Accepting mod suggestions ends on Thursday near the update "deadline"
12. Custom recipes, structures, etc. should be added in a datapack, instead of a big KubeJS file. An exception can be made for repetitive recipes, such as Steel Armor Blocks letters. This makes the custom content more maintainable and easier to look around.

