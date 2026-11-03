from __future__ import annotations

import shutil
import subprocess
import unittest

from codex_tools import viewer


class RefreshMotionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for animation controller checks')
    def test_easing_angle_continuity_restarts_and_reduced_motion(self):
        script = r'''
const assert = require('node:assert/strict');
let now=0, reduced=false, serial=0;
const frames=new Map();
const icons=[{style:{}},{style:{}},{style:{}}];
const performance={now:()=>now};
const window={
  matchMedia:()=>({matches:reduced}),
  requestAnimationFrame:fn=>{frames.set(++serial,fn);return serial;},
  cancelAnimationFrame:id=>frames.delete(id),
};
const document={querySelectorAll:()=>icons};
function tick(time){now=time; const pending=[...frames.values()];frames.clear();for(const fn of pending)fn(now);}
function near(actual,expected){assert.ok(Math.abs(actual-expected)<1e-7,`${actual} != ${expected}`);}
'''+viewer._REFRESH_MOTION_JS+r'''
setRefreshMotion(false);
assert.equal(frames.size,0);
setRefreshMotion(true);
near(refreshMotionAt(0).speed,0);
near(refreshMotionAt(75).speed,225);
near(refreshMotionAt(150).speed,450);
tick(950);
const beforeStop=refreshMotionAt(now);
setRefreshMotion(false);
near(refreshMotionAt(now).angle,beforeStop.angle%360);
near(refreshMotionAt(now).speed,450);
near(refreshMotionAt(1050).speed,225);
tick(1150);
assert.equal(frames.size,0);
near(refreshMotionAt(now).speed,0);
const settled=icons[0].style.transform;
assert.notEqual(settled,'rotate(0deg)');
assert.ok(icons.every(icon=>icon.style.transform===settled));
now=2000;
const restingAngle=refreshMotionAt(now).angle;
setRefreshMotion(true);
near(refreshMotionAt(now).angle,restingAngle);
tick(2050);
const beforeBrake=refreshMotionAt(now);
setRefreshMotion(false);
near(refreshMotionAt(now).angle,beforeBrake.angle);
near(refreshMotionAt(now).speed,beforeBrake.speed);
// A new refresh during deceleration continues from the current speed and angle.
tick(2100);
const duringBrake=refreshMotionAt(now);
setRefreshMotion(true);
near(refreshMotionAt(now).angle,duringBrake.angle);
near(refreshMotionAt(now).speed,duringBrake.speed);
assert.equal(frames.size,1);
reduced=true;
tick(2120);
assert.equal(frames.size,0);
const reducedAngle=icons[0].style.transform;
setRefreshMotion(false);
setRefreshMotion(true);
assert.equal(frames.size,0);
assert.equal(icons[0].style.transform,reducedAngle);
near(refreshMotionAt(now+1000).speed,0);
'''
        result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
