package com.daytrader.nse

import android.app.Activity
import android.graphics.Typeface
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/** One screen. Every button runs the pack's own nse.py through Python inside the app. Plans only: no orders. */
class MainActivity : Activity() {
    private lateinit var out: TextView
    private lateinit var symbol: EditText
    private lateinit var limits: EditText
    private lateinit var limitsBox: LinearLayout
    private val buttons = mutableListOf<Button>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        val d = resources.displayMetrics.density
        fun px(n: Int) = (n * d).toInt()

        fun button(label: String, onClick: () -> Unit) = Button(this).apply {
            text = label
            isAllCaps = false
            setOnClickListener { onClick() }
            buttons.add(this)
        }
        fun row(vararg views: View) = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            views.forEach { addView(it, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)) }
        }

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(px(12), px(40), px(12), px(12))
        }
        root.addView(TextView(this).apply {
            text = "NSE Day Trade Plan (IST, rupees)\nPlans only. Never places an order. Not financial advice."
            textSize = 14f
        })
        root.addView(row(
            button("Clock") { run("clock") },
            button("Build NSE list") { run("universe") },
        ))
        root.addView(row(
            button("Scan daily") { run("scan", "daily") },
            button("Scan opening range") { run("scan", "orb") },
        ))
        symbol = EditText(this).apply {
            hint = "NSE symbol, e.g. RELIANCE"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS
            setSingleLine()
        }
        root.addView(row(symbol, button("Plan") { plan() }))
        root.addView(button("My limits") { toggleLimits() })

        limits = EditText(this).apply {
            typeface = Typeface.MONOSPACE
            textSize = 12f
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
            minLines = 8
        }
        limitsBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            addView(limits)
            addView(button("Save limits") { saveLimits() })
        }
        root.addView(limitsBox)

        out = TextView(this).apply {
            typeface = Typeface.MONOSPACE
            textSize = 12f
            setTextIsSelectable(true)
            text = "Set My limits first, then Build NSE list (once a week), then Scan daily and Plan.\n" +
                "Prices are free Yahoo data and can lag. Confirm in your broker app before acting."
        }
        root.addView(ScrollView(this).apply { addView(out) },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        setContentView(root)
    }

    private val dir get() = filesDir.absolutePath
    private fun py() = Python.getInstance().getModule("bridge")

    private fun busy(on: Boolean) {
        buttons.forEach { it.isEnabled = !on }
        if (on) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    private fun run(vararg args: String) {
        busy(true)
        out.text = "Running: ${args.joinToString(" ")}\nScans can take several minutes. Keep the app open."
        Thread {
            val text = try {
                py().callAttr("run", dir, args.joinToString("\n")).toString()
            } catch (e: Exception) {
                "Error: ${e.message}"
            }
            runOnUiThread { out.text = text; busy(false) }
        }.start()
    }

    private fun plan() {
        val s = symbol.text.toString().trim().uppercase().removeSuffix(".NS")
        if (s.isEmpty()) {
            Toast.makeText(this, "Type an NSE symbol first", Toast.LENGTH_SHORT).show()
            return
        }
        run("plan", s)
    }

    private fun toggleLimits() {
        if (limitsBox.visibility == View.VISIBLE) {
            limitsBox.visibility = View.GONE
            return
        }
        Thread {
            val text = try { py().callAttr("read_config", dir).toString() } catch (e: Exception) { "Error: ${e.message}" }
            runOnUiThread { limits.setText(text); limitsBox.visibility = View.VISIBLE }
        }.start()
    }

    private fun saveLimits() {
        val text = limits.text.toString()
        Thread {
            val msg = try { py().callAttr("write_config", dir, text).toString() } catch (e: Exception) { "Error: ${e.message}" }
            runOnUiThread {
                if (msg.isEmpty()) {
                    out.text = "Limits saved."
                    limitsBox.visibility = View.GONE
                } else {
                    out.text = msg
                }
            }
        }.start()
    }
}
